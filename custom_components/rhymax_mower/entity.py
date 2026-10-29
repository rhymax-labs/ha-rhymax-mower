"""Base entity for Rhymax Mower."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import RhymaxMowerState
from .const import DOMAIN
from .coordinator import RhymaxConfigEntry, RhymaxCoordinator


@callback
def async_add_mower_entities(
    entry: RhymaxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entities_for_mower: Callable[[RhymaxCoordinator, str], Iterable[Entity]],
) -> None:
    """Add entities for every mower, including mowers added to the account later."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_mowers() -> None:
        new_ids = coordinator.data.devices.keys() - known
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(
            [
                entity
                for mower_id in sorted(new_ids)
                for entity in entities_for_mower(coordinator, mower_id)
            ]
        )

    _add_new_mowers()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_mowers))


class RhymaxEntity(CoordinatorEntity[RhymaxCoordinator]):
    """An entity of one mower.

    Unavailable while the mower is offline, since Rhymax cloud then reports no
    current values.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: RhymaxCoordinator, mower_id: str, key: str | None = None
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.mower_id = mower_id
        self._attr_unique_id = mower_id if key is None else f"{mower_id}_{key}"
        device = coordinator.data.devices[mower_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mower_id)},
            name=device.name,
            manufacturer=device.manufacturer,
            model=device.model,
            model_id=device.product,
        )

    @property
    def mower_state(self) -> RhymaxMowerState | None:
        """Return the latest state of the mower, if known."""
        return self.coordinator.data.states.get(self.mower_id)

    @property
    def available(self) -> bool:
        """Return if the mower is online."""
        state = self.mower_state
        return super().available and state is not None and state.online
