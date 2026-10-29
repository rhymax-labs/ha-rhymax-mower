"""Client for the Rhymax cloud Home Assistant API."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout, hdrs

from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from .const import API_BASE_URL

REQUEST_TIMEOUT = ClientTimeout(total=30)


class RhymaxError(Exception):
    """Base class for errors talking to Rhymax cloud."""


class RhymaxConnectionError(RhymaxError):
    """Rhymax cloud could not be reached."""


class RhymaxAuthError(RhymaxError):
    """The account link is no longer accepted; the user has to sign in again."""


class RhymaxNotAvailableError(RhymaxError):
    """Home Assistant linking is not available on Rhymax cloud."""


class RhymaxApiError(RhymaxError):
    """Rhymax cloud answered a request with an error."""

    def __init__(self, status: int, code: str | None = None) -> None:
        """Initialize the error."""
        super().__init__(f"HTTP {status}" if code is None else f"HTTP {status} ({code})")
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class RhymaxDevice:
    """A mower owned by the linked account."""

    id: str
    name: str
    manufacturer: str
    model: str | None
    product: str | None


@dataclass(frozen=True, slots=True)
class RhymaxAccount:
    """The linked account and the mowers it owns."""

    account_id: str
    devices: list[RhymaxDevice]


@dataclass(frozen=True, slots=True)
class RhymaxMowerState:
    """Latest state of a mower.

    While the mower is offline only ``online`` is set: Rhymax cloud does not
    report last-known values as if they were current.
    """

    online: bool
    activity: str | None
    battery_level: int | None
    is_docked: bool | None
    is_charging: bool | None
    progress_percent: int | None


class AbstractAuth(ABC):
    """Provides access tokens for Rhymax cloud requests."""

    @abstractmethod
    async def async_get_access_token(self) -> str:
        """Return a valid access token."""

    async def async_invalidate_access_token(self, access_token: str) -> None:
        """Forget an access token that Rhymax cloud rejected.

        Implementations that can refresh tokens return a new one from the next
        async_get_access_token call.
        """


class StaticTokenAuth(AbstractAuth):
    """A fixed access token, such as the one issued while linking the account."""

    def __init__(self, access_token: str) -> None:
        """Initialize the auth."""
        self._access_token = access_token

    async def async_get_access_token(self) -> str:
        """Return the access token."""
        return self._access_token


class ConfigEntryAuth(AbstractAuth):
    """Tokens of a config entry, refreshed and stored by its OAuth2 session."""

    def __init__(self, oauth_session: OAuth2Session) -> None:
        """Initialize the auth."""
        self._oauth_session = oauth_session

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing it when it has expired.

        Refresh tokens rotate: the session stores the new one on the entry.
        """
        await self._oauth_session.async_ensure_token_valid()
        return cast(str, self._oauth_session.token["access_token"])

    async def async_invalidate_access_token(self, access_token: str) -> None:
        """Mark the stored token expired so the session refreshes it."""
        session = self._oauth_session
        token = session.token
        if token["access_token"] != access_token:
            return  # A concurrent request already refreshed it.
        session.hass.config_entries.async_update_entry(
            session.config_entry,
            data={**session.config_entry.data, "token": {**token, "expires_at": 0}},
        )


class RhymaxCloudApi:
    """Rhymax cloud Home Assistant API.

    Only mowers owned by the linked account are listed and can be controlled.
    """

    def __init__(self, session: ClientSession, auth: AbstractAuth) -> None:
        """Initialize the API client."""
        self._session = session
        self._auth = auth

    async def async_get_account(self) -> RhymaxAccount:
        """Return the account id and the mowers it owns."""
        data = await self._request("GET", "/api/v1/homeassistant/devices")
        try:
            return RhymaxAccount(
                account_id=str(data["account_id"]),
                devices=[_parse_device(item) for item in data["devices"]],
            )
        except (AttributeError, KeyError, TypeError) as err:
            raise RhymaxApiError(HTTPStatus.OK, "invalid_response") from err

    async def async_get_state(self, mower_id: str) -> RhymaxMowerState:
        """Return the latest state of a mower."""
        data = await self._request("GET", f"{_mower_path(mower_id)}/state")
        if not isinstance(data, dict):
            raise RhymaxApiError(HTTPStatus.OK, "invalid_response")
        return RhymaxMowerState(
            online=data.get("online") is True,
            activity=_str_or_none(data.get("activity")),
            battery_level=_int_or_none(data.get("battery_level")),
            is_docked=_bool_or_none(data.get("is_docked")),
            is_charging=_bool_or_none(data.get("is_charging")),
            progress_percent=_int_or_none(data.get("progress_percent")),
        )

    async def async_send_command(self, mower_id: str, command: str) -> None:
        """Send start, stop, pause, resume or dock to a mower.

        Success means the mower received the command; whether it acted on it
        shows in its next state.
        """
        await self._request(
            "POST", f"{_mower_path(mower_id)}/commands", {"command": command}
        )

    async def async_disconnect(self) -> None:
        """Unlink Home Assistant from the account and revoke its tokens."""
        await self._request("POST", "/api/v1/homeassistant/disconnect")

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        access_token = await self._async_access_token()
        status, body = await self._send(method, path, access_token, payload)
        if status == HTTPStatus.UNAUTHORIZED:
            # Revoked early, or the clocks disagree: refresh once and retry.
            await self._auth.async_invalidate_access_token(access_token)
            retry_token = await self._async_access_token()
            if retry_token != access_token:
                status, body = await self._send(method, path, retry_token, payload)
        if status == HTTPStatus.UNAUTHORIZED:
            raise RhymaxAuthError("Rhymax cloud no longer accepts the account link")
        if status >= HTTPStatus.BAD_REQUEST:
            code = _error_code(body)
            if status == HTTPStatus.SERVICE_UNAVAILABLE and code == "not_configured":
                raise RhymaxNotAvailableError(
                    "Home Assistant linking is not available yet"
                )
            raise RhymaxApiError(status, code)
        return body

    async def _async_access_token(self) -> str:
        try:
            return await self._auth.async_get_access_token()
        except ClientResponseError as err:
            if (
                HTTPStatus.BAD_REQUEST <= err.status < HTTPStatus.INTERNAL_SERVER_ERROR
                and err.status != HTTPStatus.TOO_MANY_REQUESTS
            ):
                # The refresh token or the Application Credentials were rejected.
                raise RhymaxAuthError(
                    f"Rhymax cloud did not renew the account link (HTTP {err.status})"
                ) from err
            raise RhymaxConnectionError(
                f"Could not renew the account link (HTTP {err.status})"
            ) from err
        except (ClientError, TimeoutError) as err:
            raise RhymaxConnectionError(
                f"Could not renew the account link: {err}"
            ) from err

    async def _send(
        self,
        method: str,
        path: str,
        access_token: str,
        payload: dict[str, Any] | None,
    ) -> tuple[int, Any]:
        try:
            async with self._session.request(
                method,
                f"{API_BASE_URL}{path}",
                json=payload,
                headers={hdrs.AUTHORIZATION: f"Bearer {access_token}"},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = None
                return response.status, body
        except (ClientError, TimeoutError) as err:
            raise RhymaxConnectionError(
                f"Error talking to Rhymax cloud: {err!r}"
            ) from err


def _mower_path(mower_id: str) -> str:
    return f"/api/v1/homeassistant/devices/{quote(mower_id, safe='')}"


def _parse_device(item: dict[str, Any]) -> RhymaxDevice:
    mower_id = str(item["id"])
    return RhymaxDevice(
        id=mower_id,
        name=_str_or_none(item.get("name")) or f"Mower {mower_id[-4:].upper()}",
        manufacturer=_str_or_none(item.get("manufacturer")) or "RoboUP",
        model=_str_or_none(item.get("model")) or None,
        product=_str_or_none(item.get("product")) or None,
    )


def _error_code(body: Any) -> str | None:
    """Return the machine-readable code of an error response."""
    if not isinstance(body, dict):
        return None
    code = body.get("code")
    if code is None and isinstance(detail := body.get("detail"), dict):
        code = detail.get("code")
    return _str_or_none(code)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
