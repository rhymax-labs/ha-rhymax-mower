"""Built-in PKCE OAuth2 implementation for Rhymax Mower."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import (
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_CLIENT_ID,
    OAUTH2_TOKEN,
)


async def async_ensure_oauth_implementation(hass: HomeAssistant) -> None:
    """Register the built-in PKCE OAuth2 implementation once per HA instance."""
    implementations = await config_entry_oauth2_flow.async_get_implementations(
        hass, DOMAIN
    )
    if DOMAIN in implementations:
        return
    config_entry_oauth2_flow.async_register_implementation(
        hass,
        DOMAIN,
        config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce(
            hass,
            DOMAIN,
            client_id=OAUTH2_CLIENT_ID,
            authorize_url=OAUTH2_AUTHORIZE,
            token_url=OAUTH2_TOKEN,
            client_secret="",
        ),
    )
