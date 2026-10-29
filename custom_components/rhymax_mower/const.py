"""Constants for the Rhymax Mower integration."""

from typing import Final

DOMAIN: Final = "rhymax_mower"

API_BASE_URL: Final = "https://www.cloud.iroboup.com"
WEBSOCKET_URL: Final = "wss://www.cloud.iroboup.com/api/v1/homeassistant/ws"
OAUTH2_AUTHORIZE: Final = f"{API_BASE_URL}/oauth/authorize"
OAUTH2_TOKEN: Final = f"{API_BASE_URL}/oauth/token"
OAUTH2_CLIENT_ID: Final = "homeassistant"
OAUTH2_SCOPE: Final = "smarthome"

DOCUMENTATION_URL: Final = "https://github.com/rhymax-labs/ha-rhymax-mower"

# Transport timing does not determine whether an individual mower is online.
WS_HEARTBEAT_SECONDS: Final = 30
WS_TOKEN_CHECK_SECONDS: Final = 30
WS_CONNECT_TIMEOUT_SECONDS: Final = 15
WS_CLOSE_TIMEOUT_SECONDS: Final = 5
WS_BACKOFF_MAX_SECONDS: Final = 60
WS_EVENT_CACHE_SIZE: Final = 1024
