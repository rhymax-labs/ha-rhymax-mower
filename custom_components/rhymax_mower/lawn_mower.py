"""Lawn mower entity scaffold — no cloud calls yet."""

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([RhymaxMowerEntity(entry)])


class RhymaxMowerEntity(LawnMowerEntity):
    _attr_name = "Rhymax Mower"
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )
    _attr_activity = LawnMowerActivity.DOCKED

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_mower"

    async def async_start_mowing(self) -> None:
        return

    async def async_pause(self) -> None:
        return

    async def async_dock(self) -> None:
        return
