"""The Rhymax Mower integration."""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import ClientError

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    HomeAssistantError,
)
from homeassistant.helpers import config_entry_oauth2_flow, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ConfigEntryAuth, RhymaxCloudApi, RhymaxError, StaticTokenAuth
from .const import DOMAIN
from .coordinator import RhymaxConfigEntry, RhymaxCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.LAWN_MOWER,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: RhymaxConfigEntry) -> bool:
    """Set up Rhymax Mower from a config entry."""
    if "auth_implementation" not in entry.data:
        # Entries created by 0.0.x hold no account link.
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="relink_required"
        )
    try:
        implementation = (
            await config_entry_oauth2_flow.async_get_config_entry_implementation(
                hass, entry
            )
        )
    except ValueError as err:
        # The Application Credentials of this entry no longer exist.
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="credentials_missing"
        ) from err

    oauth_session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    api = RhymaxCloudApi(async_get_clientsession(hass), ConfigEntryAuth(oauth_session))
    coordinator = RhymaxCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RhymaxConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: RhymaxConfigEntry) -> None:
    """Unlink Home Assistant from the RoboUP account when the entry is deleted."""
    if not (token := entry.data.get("token")):
        return
    try:
        implementation = (
            await config_entry_oauth2_flow.async_get_config_entry_implementation(
                hass, entry
            )
        )
    except (ValueError, HomeAssistantError) as err:
        _LOGGER.warning("Could not unlink Home Assistant from RoboUP account: %s", err)
        return

    try:
        async with asyncio.timeout(30):
            # Home Assistant has already dropped the entry, so a refreshed
            # token is only used for this request and not stored.
            if token["expires_at"] < time.time() + 60:
                token = await implementation.async_refresh_token(token)
            api = RhymaxCloudApi(
                async_get_clientsession(hass), StaticTokenAuth(token["access_token"])
            )
            await api.async_disconnect()
    except (ClientError, TimeoutError, RhymaxError) as err:
        _LOGGER.warning("Could not unlink Home Assistant from RoboUP account: %s", err)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: RhymaxConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow removing a mower that is no longer on the account."""
    return not any(
        domain == DOMAIN and mower_id in entry.runtime_data.data.devices
        for domain, mower_id in device_entry.identifiers
    )
