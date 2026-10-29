"""Data update coordinator for Rhymax Mower."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    RhymaxApiError,
    RhymaxAuthError,
    RhymaxCloudApi,
    RhymaxDevice,
    RhymaxError,
    RhymaxMowerState,
    RhymaxNotAvailableError,
)
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

type RhymaxConfigEntry = ConfigEntry[RhymaxCoordinator]


@dataclass(frozen=True, slots=True)
class RhymaxData:
    """Mowers on the account, by id, and the latest state of each."""

    devices: dict[str, RhymaxDevice]
    states: dict[str, RhymaxMowerState]


class RhymaxCoordinator(DataUpdateCoordinator[RhymaxData]):
    """Polls the mower list and the state of every mower."""

    config_entry: RhymaxConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: RhymaxConfigEntry, api: RhymaxCloudApi
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api

    async def _async_update_data(self) -> RhymaxData:
        try:
            account = await self.api.async_get_account()
            devices = {device.id: device for device in account.devices}
            states: dict[str, RhymaxMowerState] = {}
            for mower_id, device in devices.items():
                try:
                    states[mower_id] = await self.api.async_get_state(mower_id)
                except RhymaxApiError as err:
                    # Only this mower is affected; its entities become unavailable.
                    _LOGGER.debug("Could not get the state of %s: %s", device.name, err)
        except RhymaxAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except RhymaxNotAvailableError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="linking_unavailable"
            ) from err
        except RhymaxError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return RhymaxData(devices=devices, states=states)
