"""Config flow for Rhymax Mower."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    RhymaxAuthError,
    RhymaxCloudApi,
    RhymaxError,
    RhymaxNotAvailableError,
    StaticTokenAuth,
)
from .const import DOMAIN, OAUTH2_SCOPE

_LOGGER = logging.getLogger(__name__)


class OAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Link a RoboUP account through Rhymax cloud."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        """Return the logger."""
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Request the smart home scope."""
        return {"scope": OAUTH2_SCOPE}

    async def async_step_creation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange the authorization code for tokens."""
        result = await super().async_step_creation(user_input)
        if (
            result["type"] is FlowResultType.ABORT
            and result["reason"] == "oauth_unauthorized"
        ):
            # Rhymax cloud refused the client ID or secret: they are wrong, or
            # Home Assistant linking is not available yet.
            return self.async_abort(reason="link_rejected")
        return result

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Create the entry for the linked account, or update it when re-linking."""
        api = RhymaxCloudApi(
            async_get_clientsession(self.hass),
            StaticTokenAuth(data["token"]["access_token"]),
        )
        try:
            account = await api.async_get_account()
        except RhymaxNotAvailableError:
            return self.async_abort(reason="linking_unavailable")
        except RhymaxAuthError:
            return self.async_abort(reason="invalid_auth")
        except RhymaxError as err:
            _LOGGER.warning("Could not get the mower list from Rhymax cloud: %s", err)
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(account.account_id)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="RoboUP account", data=data)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-linking when the account link stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to sign in to the RoboUP account again."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()
