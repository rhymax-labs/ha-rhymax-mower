"""Constants for the Rhymax Mower integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "rhymax_mower"

API_BASE_URL: Final = "https://www.cloud.iroboup.com"
OAUTH2_AUTHORIZE: Final = f"{API_BASE_URL}/oauth/authorize"
OAUTH2_TOKEN: Final = f"{API_BASE_URL}/oauth/token"
OAUTH2_CLIENT_ID: Final = "homeassistant"
OAUTH2_SCOPE: Final = "smarthome"

DOCUMENTATION_URL: Final = "https://github.com/rhymax-labs/ha-rhymax-mower"

# Rhymax cloud asks clients not to poll mower state more often than every 30 s.
UPDATE_INTERVAL: Final = timedelta(seconds=30)
