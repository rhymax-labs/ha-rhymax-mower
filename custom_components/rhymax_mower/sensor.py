"""Sensor platform for Rhymax Mower."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import RhymaxMowerState
from .coordinator import RhymaxConfigEntry, RhymaxCoordinator
from .entity import RhymaxEntity, async_add_mower_entities

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class RhymaxSensorEntityDescription(SensorEntityDescription):
    """Describes a Rhymax mower sensor."""

    value_fn: Callable[[RhymaxMowerState], int | None]


SENSORS: tuple[RhymaxSensorEntityDescription, ...] = (
    RhymaxSensorEntityDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: state.battery_level,
    ),
    RhymaxSensorEntityDescription(
        key="mowing_progress",
        translation_key="mowing_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: state.progress_percent,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RhymaxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for every mower."""
    async_add_mower_entities(
        entry,
        async_add_entities,
        lambda coordinator, mower_id: (
            RhymaxSensor(coordinator, mower_id, description) for description in SENSORS
        ),
    )


class RhymaxSensor(RhymaxEntity, SensorEntity):
    """A sensor of a Rhymax mower."""

    entity_description: RhymaxSensorEntityDescription

    def __init__(
        self,
        coordinator: RhymaxCoordinator,
        mower_id: str,
        description: RhymaxSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, mower_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | None:
        """Return the sensor value."""
        if (state := self.mower_state) is None:
            return None
        return self.entity_description.value_fn(state)
