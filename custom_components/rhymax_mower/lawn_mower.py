"""Lawn mower platform for Rhymax Mower."""

from __future__ import annotations

from http import HTTPStatus

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import (
    RhymaxApiError,
    RhymaxAuthError,
    RhymaxError,
    RhymaxMowerState,
    RhymaxNotAvailableError,
)
from .const import DOMAIN
from .coordinator import RhymaxConfigEntry
from .entity import RhymaxEntity, async_add_mower_entities

PARALLEL_UPDATES = 1

_ACTIVITIES = {
    "error": LawnMowerActivity.ERROR,
    "returning": LawnMowerActivity.RETURNING,
    "docked": LawnMowerActivity.DOCKED,
    "paused": LawnMowerActivity.PAUSED,
    "mowing": LawnMowerActivity.MOWING,
}

# Home Assistant 2026.10 added an idle activity and a stop action. On older
# releases a mower standing still away from the dock shows as paused, and stop
# is not offered.
_IDLE = getattr(LawnMowerActivity, "IDLE", LawnMowerActivity.PAUSED)
_STOP = getattr(LawnMowerEntityFeature, "STOP", LawnMowerEntityFeature(0))

# Codes of rejected commands, mapped to exception translation keys.
_COMMAND_ERRORS = {
    "device_offline": "mower_offline",
    "device_busy": "mower_busy",
    "device_timeout": "mower_timeout",
    "device_error": "command_rejected",
    "geo_restricted": "region_restricted",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RhymaxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a lawn mower entity for every mower."""
    async_add_mower_entities(
        entry,
        async_add_entities,
        lambda coordinator, mower_id: [RhymaxLawnMower(coordinator, mower_id)],
    )


def mower_activity(state: RhymaxMowerState) -> LawnMowerActivity | None:
    """Map the activity reported by Rhymax cloud to a lawn mower activity."""
    if (activity := _ACTIVITIES.get(state.activity or "")) is not None:
        return activity
    if state.is_docked or state.is_charging:
        return LawnMowerActivity.DOCKED
    if state.activity is None:
        return None
    # "idle", other jobs such as mapping or remote control, and values added later.
    return _IDLE


def _command_error_key(err: RhymaxError) -> str:
    if isinstance(err, RhymaxNotAvailableError):
        return "linking_unavailable"
    if isinstance(err, RhymaxApiError):
        if err.status == HTTPStatus.NOT_FOUND:
            return "mower_not_found"
        return _COMMAND_ERRORS.get(err.code or "", "command_failed")
    return "cannot_connect"


class RhymaxLawnMower(RhymaxEntity, LawnMowerEntity):
    """A Rhymax mower."""

    _attr_name = None
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
        | _STOP
    )

    @property
    def activity(self) -> LawnMowerActivity | None:
        """Return the current activity."""
        if (state := self.mower_state) is None:
            return None
        return mower_activity(state)

    async def async_start_mowing(self) -> None:
        """Start mowing, or resume the paused mowing task."""
        state = self.mower_state
        paused = state is not None and state.activity == "paused"
        await self._async_send_command("resume" if paused else "start")

    async def async_pause(self) -> None:
        """Pause mowing."""
        await self._async_send_command("pause")

    async def async_dock(self) -> None:
        """Return to the dock."""
        await self._async_send_command("dock")

    async def async_stop(self) -> None:
        """Stop mowing."""
        await self._async_send_command("stop")

    async def _async_send_command(self, command: str) -> None:
        try:
            await self.coordinator.api.async_send_command(self.mower_id, command)
        except RhymaxAuthError as err:
            self.coordinator.async_auth_failed(err)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except RhymaxError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key=_command_error_key(err)
            ) from err
        await self.coordinator.async_confirm_command(self.mower_id, command)
