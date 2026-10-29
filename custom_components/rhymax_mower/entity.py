"""Base entity for Rhymax Mower."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    async_get_current_platform,
)
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
    """Keep this platform's entities aligned with the owner's mower inventory."""
    coordinator = entry.runtime_data
    platform = async_get_current_platform()
    entity_registry = er.async_get(coordinator.hass)
    device_registry = dr.async_get(coordinator.hass)
    known: dict[str, list[Entity]] = {}
    inventory = dict(coordinator.data.devices)
    task: asyncio.Task[None] | None = None
    dirty = False
    stopped = False

    @callback
    def _reconcile_registries() -> None:
        """Prune only this entry/platform and preserve shared devices and names."""
        devices = coordinator.data.devices
        for registry_entry in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        ):
            if (
                registry_entry.platform == DOMAIN
                and registry_entry.domain == platform.domain
                and registry_entry.unique_id.partition("_")[0] not in devices
            ):
                entity_registry.async_remove(registry_entry.entity_id)

        remaining_device_ids = {
            registry_entry.device_id
            for registry_entry in er.async_entries_for_config_entry(
                entity_registry, entry.entry_id
            )
        }
        for device_entry in dr.async_entries_for_config_entry(
            device_registry, entry.entry_id
        ):
            mower_ids = {
                mower_id
                for domain, mower_id in device_entry.identifiers
                if domain == DOMAIN
            }
            if not mower_ids:
                continue
            if not mower_ids.intersection(devices):
                if device_entry.id not in remaining_device_ids:
                    if getattr(device_entry, "config_entry_id", None) == entry.entry_id:
                        # Current HA gives each config entry its own device.
                        device_registry.async_remove_device(device_entry.id)
                    else:
                        # Older HA versions can share a device across entries.
                        device_registry.async_update_device(
                            device_entry.id, remove_config_entry_id=entry.entry_id
                        )
                continue
            device = devices[next(iter(mower_ids.intersection(devices)))]
            device_registry.async_update_device(
                device_entry.id,
                name=device.name,
                manufacturer=device.manufacturer,
                model=device.model,
                model_id=device.product,
            )

    async def _async_reconcile() -> None:
        """Finish removals before re-adding a mower with the same unique ids."""
        nonlocal dirty, task
        try:
            while dirty and not stopped:
                dirty = False
                for mower_id in known.keys() - coordinator.data.devices.keys():
                    for entity in known.pop(mower_id):
                        # Disabled entities were registered but never added to HA.
                        if platform.entities.get(entity.entity_id) is entity:
                            await entity.async_remove(force_remove=True)
                if stopped:
                    return
                _reconcile_registries()
                new_entities: list[Entity] = []
                for mower_id in sorted(coordinator.data.devices.keys() - known.keys()):
                    known[mower_id] = list(entities_for_mower(coordinator, mower_id))
                    new_entities.extend(known[mower_id])
                if new_entities:
                    # Unlike the setup callback, this is awaitable. An unbind
                    # arriving during addition is reconciled after it completes.
                    await platform.async_add_entities(new_entities)
        finally:
            task = None

    @callback
    def _inventory_updated() -> None:
        nonlocal inventory, dirty, task
        if stopped or inventory == coordinator.data.devices:
            return
        inventory = dict(coordinator.data.devices)
        dirty = True
        if task is None:
            task = entry.async_create_background_task(
                coordinator.hass,
                _async_reconcile(),
                f"{DOMAIN} {platform.domain} inventory",
                eager_start=False,
            )

    @callback
    def _stop() -> None:
        nonlocal stopped
        stopped = True
        if task is not None:
            task.cancel()

    _reconcile_registries()
    for mower_id in sorted(coordinator.data.devices):
        known[mower_id] = list(entities_for_mower(coordinator, mower_id))
    async_add_entities([entity for entities in known.values() for entity in entities])
    entry.async_on_unload(coordinator.async_add_listener(_inventory_updated))
    entry.async_on_unload(_stop)


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
        self._update_device_info()

    @callback
    def _update_device_info(self) -> None:
        """Keep metadata used by entity registration current after a rename."""
        if (device := self.coordinator.data.devices.get(self.mower_id)) is None:
            return
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.mower_id)},
            name=device.name,
            manufacturer=device.manufacturer,
            model=device.model,
            model_id=device.product,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_device_info()
        super()._handle_coordinator_update()

    @property
    def mower_state(self) -> RhymaxMowerState | None:
        """Return the latest state of the mower, if known."""
        if self.mower_id not in self.coordinator.data.devices:
            return None
        return self.coordinator.data.states.get(self.mower_id)

    @property
    def available(self) -> bool:
        """Return if the mower is online."""
        state = self.mower_state
        return super().available and state is not None and state.online
