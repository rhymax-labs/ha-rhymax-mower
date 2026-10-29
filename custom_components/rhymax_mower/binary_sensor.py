"""Binary sensor platform for Rhymax Mower."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RhymaxConfigEntry, RhymaxCoordinator
from .entity import RhymaxEntity, async_add_mower_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RhymaxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors for every mower."""
    async_add_mower_entities(
        entry,
        async_add_entities,
        lambda coordinator, mower_id: [
            RhymaxChargingSensor(coordinator, mower_id),
            RhymaxConnectivitySensor(coordinator, mower_id),
        ],
    )


class RhymaxChargingSensor(RhymaxEntity, BinarySensorEntity):
    """Whether the mower is charging."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: RhymaxCoordinator, mower_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, mower_id, "charging")

    @property
    def is_on(self) -> bool | None:
        """Return if the mower is charging."""
        if (state := self.mower_state) is None:
            return None
        return state.is_charging


class RhymaxConnectivitySensor(RhymaxEntity, BinarySensorEntity):
    """Whether the mower is online; stays available while the mower is offline."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RhymaxCoordinator, mower_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, mower_id, "online")

    @property
    def available(self) -> bool:
        """Return if the state of the mower is known."""
        return self.coordinator.last_update_success and self.mower_state is not None

    @property
    def is_on(self) -> bool | None:
        """Return if the mower is online."""
        if (state := self.mower_state) is None:
            return None
        return state.online
