"""Inventory changes remove, restore, and rename real Home Assistant entities."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rhymax_mower import binary_sensor, lawn_mower, sensor
from custom_components.rhymax_mower.const import DOMAIN
from custom_components.rhymax_mower.coordinator import RhymaxData

from .conftest import MOWER_A, MOWER_B, MOWER_C


def _device_for_entry(registry, entry_id, mower_id):
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(registry, entry_id)
            if (DOMAIN, mower_id) in device.identifiers
        ),
        None,
    )


@pytest.fixture
async def mower_platform(hass, entry, coordinator) -> AsyncGenerator[EntityPlatform]:
    """Use the actual entity platform and registries, without cloud transport."""
    assert await async_setup_component(hass, "lawn_mower", {})
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="lawn_mower",
        platform_name=DOMAIN,
        platform=lawn_mower,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    assert await platform.async_setup_entry(entry)
    await hass.async_block_till_done()
    yield platform
    await platform.async_reset()


async def test_unbind_removes_entity_and_rebind_adds_once(
    hass, entry, coordinator, mower_platform, entity_registry, device_registry
) -> None:
    original = coordinator.data
    entity_id = entity_registry.async_get_entity_id("lawn_mower", DOMAIN, MOWER_A)
    assert hass.states.get(entity_id) is not None

    coordinator.async_set_updated_data(
        RhymaxData(
            devices={MOWER_B: original.devices[MOWER_B]},
            states={MOWER_B: original.states[MOWER_B]},
        )
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(entity_id) is None
    assert entity_registry.async_get(entity_id) is None
    assert _device_for_entry(device_registry, entry.entry_id, MOWER_A) is None

    coordinator.async_set_updated_data(original)
    await hass.async_block_till_done(wait_background_tasks=True)
    restored_id = entity_registry.async_get_entity_id("lawn_mower", DOMAIN, MOWER_A)
    assert hass.states.get(restored_id) is not None
    assert len(mower_platform.entities) == 2


async def test_rename_preserves_user_name_and_updates_device_metadata(
    hass, entry, coordinator, mower_platform, device_registry
) -> None:
    device = _device_for_entry(device_registry, entry.entry_id, MOWER_A)
    device_registry.async_update_device(device.id, name_by_user="My mower")
    current = coordinator.data
    renamed = replace(current.devices[MOWER_A], name="New cloud name")
    coordinator.async_set_updated_data(
        RhymaxData(devices={**current.devices, MOWER_A: renamed}, states=current.states)
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    device = device_registry.async_get(device.id)
    assert device.name == "New cloud name"
    assert device.name_by_user == "My mower"
    assert len(mower_platform.entities) == 2


async def test_unbind_preserves_another_integrations_device_with_same_identifier(
    hass, entry, coordinator, mower_platform, entity_registry, device_registry
) -> None:
    other_entry = MockConfigEntry(domain="other_integration")
    other_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={(DOMAIN, MOWER_A)},
        name="Other integration mower",
    )
    unrelated_entity = entity_registry.async_get_or_create(
        "sensor",
        "other_integration",
        "other_unique_id",
        config_entry=other_entry,
        device_id=device.id,
    )

    current = coordinator.data
    coordinator.async_set_updated_data(
        RhymaxData(
            devices={MOWER_B: current.devices[MOWER_B]},
            states={MOWER_B: current.states[MOWER_B]},
        )
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    device = device_registry.async_get(device.id)
    assert device is not None
    assert device.config_entry_id == other_entry.entry_id
    assert entity_registry.async_get(unrelated_entity.entity_id) is not None
    assert not any(
        item.unique_id == MOWER_A
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    )


async def test_startup_prunes_unbound_registry_entities(
    hass, entry, coordinator, entity_registry, device_registry
) -> None:
    missing_id = "010203040506"
    missing_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, missing_id)},
        name="Old mower",
    )
    missing_entity = entity_registry.async_get_or_create(
        "lawn_mower",
        DOMAIN,
        missing_id,
        config_entry=entry,
        device_id=missing_device.id,
    )
    assert await async_setup_component(hass, "lawn_mower", {})
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="lawn_mower",
        platform_name=DOMAIN,
        platform=lawn_mower,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    try:
        assert await platform.async_setup_entry(entry)
        await hass.async_block_till_done()
        assert entity_registry.async_get(missing_entity.entity_id) is None
        assert device_registry.async_get(missing_device.id) is None
    finally:
        await platform.async_reset()


async def test_unbind_during_addition_leaves_no_ghost_entity(
    hass, entry, coordinator, mower_platform, entity_registry, device_registry
) -> None:
    original = coordinator.data
    adding = asyncio.Event()
    release = asyncio.Event()
    add_entities = mower_platform.async_add_entities

    async def delayed_add(entities):
        adding.set()
        await release.wait()
        await add_entities(entities)

    new_device = replace(original.devices[MOWER_A], id=MOWER_C, name="New mower")
    with patch.object(mower_platform, "async_add_entities", side_effect=delayed_add):
        coordinator.async_set_updated_data(
            RhymaxData(
                devices={**original.devices, MOWER_C: new_device},
                states=original.states,
            )
        )
        await adding.wait()
        coordinator.async_set_updated_data(original)
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert entity_registry.async_get_entity_id("lawn_mower", DOMAIN, MOWER_C) is None
    assert _device_for_entry(device_registry, entry.entry_id, MOWER_C) is None
    assert len(mower_platform.entities) == 2


async def test_unbind_removes_all_platforms_before_device(
    hass, entry, coordinator, mower_platform, entity_registry, device_registry
) -> None:
    extra_platforms = []
    try:
        for domain, module in (("sensor", sensor), ("binary_sensor", binary_sensor)):
            assert await async_setup_component(hass, domain, {})
            platform = EntityPlatform(
                hass=hass,
                logger=logging.getLogger(__name__),
                domain=domain,
                platform_name=DOMAIN,
                platform=module,
                scan_interval=timedelta(seconds=30),
                entity_namespace=None,
            )
            assert await platform.async_setup_entry(entry)
            extra_platforms.append(platform)
        await hass.async_block_till_done()
        assert (
            len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
            == 10
        )
        current = coordinator.data
        coordinator.async_set_updated_data(
            RhymaxData(
                devices={MOWER_B: current.devices[MOWER_B]},
                states={MOWER_B: current.states[MOWER_B]},
            )
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert (
            len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == 5
        )
        assert _device_for_entry(device_registry, entry.entry_id, MOWER_A) is None
        assert len(hass.states.async_all()) == 5
    finally:
        for platform in extra_platforms:
            await platform.async_reset()
