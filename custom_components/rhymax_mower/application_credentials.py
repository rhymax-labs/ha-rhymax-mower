"""Application credentials platform for Rhymax Mower."""

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import MY_AUTH_CALLBACK_PATH

from .const import DOCUMENTATION_URL, OAUTH2_AUTHORIZE, OAUTH2_CLIENT_ID, OAUTH2_TOKEN


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return the Rhymax cloud authorization server."""
    return AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN)


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Return placeholders for the Application Credentials dialog."""
    return {
        "client_id": OAUTH2_CLIENT_ID,
        "redirect_url": MY_AUTH_CALLBACK_PATH,
        "more_info_url": DOCUMENTATION_URL,
    }
