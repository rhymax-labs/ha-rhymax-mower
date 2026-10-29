"""Client for the Rhymax cloud Home Assistant API."""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

from aiohttp import (
    ClientError,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    ClientWebSocketResponse,
    hdrs,
)
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from .const import (
    API_BASE_URL,
    WEBSOCKET_URL,
    WS_CONNECT_TIMEOUT_SECONDS,
    WS_HEARTBEAT_SECONDS,
)

REQUEST_TIMEOUT = ClientTimeout(total=30)


class RhymaxError(Exception):
    """Base class for errors talking to Rhymax cloud."""


class RhymaxConnectionError(RhymaxError):
    """Rhymax cloud could not be reached."""


class RhymaxAuthError(RhymaxError):
    """The account link is no longer accepted; the user has to sign in again."""


class RhymaxNotAvailableError(RhymaxError):
    """Home Assistant linking is not available on Rhymax cloud."""


class RhymaxSubscriptionError(RhymaxError):
    """Cloud policy refused the push subscription; automatic retry is stopped."""


class RhymaxApiError(RhymaxError):
    """Rhymax cloud answered a request with an error."""

    def __init__(
        self, status: int, code: str | None = None, retry_after: float | None = None
    ) -> None:
        """Initialize the error."""
        super().__init__(
            f"HTTP {status}" if code is None else f"HTTP {status} ({code})"
        )
        self.status = status
        self.code = code
        self.retry_after = retry_after


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

    Offline snapshots retain only identity, presence and the activity timestamp.
    Dynamic fields are cleared, never populated from last-known telemetry.
    """

    id: str
    online: bool
    last_activity_ts: int | None
    activity: str | None
    task_state: int | None
    sub_state: int | None
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
        token = self._oauth_session.token
        if token.get("expires_at", 0) <= time.time() + 60:
            await self.async_invalidate_access_token(token["access_token"])
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
        self._auth_lock = asyncio.Lock()

    async def async_get_account(self) -> RhymaxAccount:
        """Return the account id and the mowers it owns."""
        data = await self._request("GET", "/api/v1/homeassistant/devices")
        try:
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("devices"), list)
                or not isinstance(data.get("account_id"), (str, int))
                or isinstance(data["account_id"], bool)
            ):
                raise RhymaxApiError(HTTPStatus.OK, "invalid_response")
            devices = [parse_device(item) for item in data["devices"]]
            if len({device.id for device in devices}) != len(devices):
                raise RhymaxApiError(HTTPStatus.OK, "invalid_response")
            return RhymaxAccount(account_id=str(data["account_id"]), devices=devices)
        except (AttributeError, KeyError, TypeError) as err:
            raise RhymaxApiError(HTTPStatus.OK, "invalid_response") from err

    async def async_get_state(self, mower_id: str) -> RhymaxMowerState:
        """Return the latest state of a mower."""
        data = await self._request("GET", f"{_mower_path(mower_id)}/state")
        return parse_state(data, expected_id=mower_id)

    async def async_get_access_token(self) -> str:
        """Get the same valid OAuth credential for REST and WebSocket."""
        async with self._auth_lock:
            return await self._async_access_token()

    async def async_refresh_access_token(self, rejected_token: str) -> str:
        """Refresh a rejected credential once, sharing concurrent rotations."""
        async with self._auth_lock:
            token = await self._async_access_token()
            if token == rejected_token:
                await self._auth.async_invalidate_access_token(rejected_token)
                token = await self._async_access_token()
            if token == rejected_token:
                raise RhymaxAuthError(
                    "Rhymax cloud did not renew the rejected account link"
                )
            return token

    async def async_connect_websocket(
        self, access_token: str
    ) -> ClientWebSocketResponse:
        """Open only the HA subscription with the shared OAuth Bearer header."""
        try:
            async with asyncio.timeout(WS_CONNECT_TIMEOUT_SECONDS):
                return await self._session.ws_connect(
                    WEBSOCKET_URL,
                    headers={hdrs.AUTHORIZATION: f"Bearer {access_token}"},
                    heartbeat=WS_HEARTBEAT_SECONDS,
                    autoping=False,
                    max_msg_size=1024 * 1024,
                )
        except ClientResponseError as err:
            if err.status == HTTPStatus.UNAUTHORIZED:
                raise RhymaxAuthError(
                    "Rhymax cloud rejected the push account link"
                ) from err
            if err.status == HTTPStatus.FORBIDDEN:
                raise RhymaxSubscriptionError(
                    "Rhymax cloud refused the push subscription"
                ) from err
            raise RhymaxConnectionError(
                f"WebSocket handshake failed (HTTP {err.status})"
            ) from err
        except (ClientError, TimeoutError) as err:
            raise RhymaxConnectionError("Could not connect to Rhymax push") from err

    async def async_send_command(self, mower_id: str, command: str) -> None:
        """Send start, stop, pause, resume or dock to a mower.

        Success means the cloud accepted the command. Device execution is
        confirmed by a later state, not by this acknowledgement.
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
        access_token = await self.async_get_access_token()
        status, body, retry_after = await self._send(
            method, path, access_token, payload
        )
        if status == HTTPStatus.UNAUTHORIZED:
            # Revoked early, or the clocks disagree: refresh once and retry.
            retry_token = await self.async_refresh_access_token(access_token)
            status, body, retry_after = await self._send(
                method, path, retry_token, payload
            )
        if status == HTTPStatus.UNAUTHORIZED:
            raise RhymaxAuthError("Rhymax cloud no longer accepts the account link")
        if status >= HTTPStatus.MULTIPLE_CHOICES:
            code = _error_code(body)
            if status == HTTPStatus.SERVICE_UNAVAILABLE and code == "not_configured":
                raise RhymaxNotAvailableError(
                    "Home Assistant linking is not available yet"
                )
            raise RhymaxApiError(status, code, retry_after)
        return body

    async def _async_access_token(self) -> str:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT.total):
                token = await self._auth.async_get_access_token()
            if not isinstance(token, str) or not token:
                raise RhymaxAuthError("The account link has no access token")
            return token
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
    ) -> tuple[int, Any, float | None]:
        try:
            async with self._session.request(
                method,
                f"{API_BASE_URL}{path}",
                json=payload,
                headers={hdrs.AUTHORIZATION: f"Bearer {access_token}"},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = None
                retry_after = None
                if value := response.headers.get(hdrs.RETRY_AFTER):
                    try:
                        retry_after = min(3600.0, max(0.0, float(value)))
                    except ValueError:
                        pass
                return response.status, body, retry_after
        except (ClientError, TimeoutError) as err:
            raise RhymaxConnectionError(
                f"Error talking to Rhymax cloud: {err!r}"
            ) from err


def _mower_path(mower_id: str) -> str:
    return f"/api/v1/homeassistant/devices/{quote(mower_id, safe='')}"


def parse_device(item: Any) -> RhymaxDevice:
    """Validate a device in the shared owner-only REST/push inventory."""
    if not isinstance(item, dict) or not _valid_id(item.get("id")):
        raise RhymaxApiError(HTTPStatus.OK, "invalid_response")
    mower_id = item["id"]
    return RhymaxDevice(
        id=mower_id,
        name=_str_or_none(item.get("name")) or f"Mower {mower_id[-4:].upper()}",
        manufacturer=_str_or_none(item.get("manufacturer")) or "RoboUP",
        model=_str_or_none(item.get("model")) or None,
        product=_str_or_none(item.get("product")) or None,
    )


def parse_state(data: Any, expected_id: str | None = None) -> RhymaxMowerState:
    """Validate the frozen HaState projection for both REST and push.

    Invalid presence must fail, not turn a transport/schema error into offline.
    Offline snapshots clear all seven dynamic fields even if a sender includes
    stale values. Extra fields are never copied into integration state.
    """
    if (
        not isinstance(data, dict)
        or not _valid_id(data.get("id"))
        or (expected_id is not None and data["id"] != expected_id)
        or type(data.get("online")) is not bool
    ):
        raise RhymaxApiError(HTTPStatus.OK, "invalid_response")
    last_activity_ts = _nullable_int(data.get("last_activity_ts"), minimum=0)
    if not data["online"]:
        return RhymaxMowerState(
            data["id"],
            False,
            last_activity_ts,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
    activity = data.get("activity")
    if activity is not None:
        if not isinstance(activity, str):
            raise RhymaxApiError(HTTPStatus.OK, "invalid_response")
        if activity not in {
            "mowing",
            "paused",
            "returning",
            "docked",
            "idle",
            "error",
            "other",
        }:
            activity = "other"
    return RhymaxMowerState(
        id=data["id"],
        online=True,
        last_activity_ts=last_activity_ts,
        activity=activity,
        task_state=_nullable_int(data.get("task_state"), minimum=0, maximum=3),
        sub_state=_nullable_int(data.get("sub_state")),
        battery_level=_nullable_int(data.get("battery_level"), minimum=0, maximum=100),
        is_docked=_nullable_bool(data.get("is_docked")),
        is_charging=_nullable_bool(data.get("is_charging")),
        progress_percent=_nullable_int(
            data.get("progress_percent"), minimum=0, maximum=100
        ),
    )


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12}", value) is not None


def _nullable_int(
    value: Any, *, minimum: int | None = None, maximum: int | None = None
) -> int | None:
    if value is None:
        return None
    if (
        type(value) is not int
        or (minimum is not None and value < minimum)
        or (maximum is not None and value > maximum)
    ):
        raise RhymaxApiError(HTTPStatus.OK, "invalid_response")
    return value


def _nullable_bool(value: Any) -> bool | None:
    if value is not None and type(value) is not bool:
        raise RhymaxApiError(HTTPStatus.OK, "invalid_response")
    return value


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
