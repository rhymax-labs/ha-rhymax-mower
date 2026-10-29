"""Config-entry ownership of transports and background tasks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.rhymax_mower import (
    PLATFORMS,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)


async def test_setup_starts_transport_after_cold_refresh(hass, entry) -> None:
    """Entities get REST initial data before the outbound channel is started."""
    coordinator = MagicMock()
    order = []
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=lambda: order.append("refresh")
    )
    coordinator.async_start = AsyncMock(side_effect=lambda: order.append("start"))
    coordinator.async_shutdown = AsyncMock()
    with (
        patch(
            "custom_components.rhymax_mower.config_entry_oauth2_flow."
            "async_get_config_entry_implementation",
            new_callable=AsyncMock,
        ),
        patch("custom_components.rhymax_mower.config_entry_oauth2_flow.OAuth2Session"),
        patch(
            "custom_components.rhymax_mower.RhymaxCoordinator", return_value=coordinator
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ) as forward,
    ):
        assert await async_setup_entry(hass, entry)
    assert entry.runtime_data is coordinator
    assert order == ["refresh", "start"]
    forward.assert_awaited_once_with(entry, PLATFORMS)


async def test_failed_first_refresh_does_not_leave_transport(hass, entry) -> None:
    """A cold-start failure never leaves a WebSocket or watchdog running."""
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=ConfigEntryNotReady
    )
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    with (
        patch(
            "custom_components.rhymax_mower.config_entry_oauth2_flow."
            "async_get_config_entry_implementation",
            new_callable=AsyncMock,
        ),
        patch("custom_components.rhymax_mower.config_entry_oauth2_flow.OAuth2Session"),
        patch(
            "custom_components.rhymax_mower.RhymaxCoordinator", return_value=coordinator
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)
    coordinator.async_start.assert_not_awaited()
    coordinator.async_shutdown.assert_awaited_once()


async def test_failed_platform_setup_closes_coordinator(hass, entry) -> None:
    """A platform exception cannot strand tasks created by cold-start reads."""
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    with (
        patch(
            "custom_components.rhymax_mower.config_entry_oauth2_flow."
            "async_get_config_entry_implementation",
            new_callable=AsyncMock,
        ),
        patch("custom_components.rhymax_mower.config_entry_oauth2_flow.OAuth2Session"),
        patch(
            "custom_components.rhymax_mower.RhymaxCoordinator", return_value=coordinator
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
            side_effect=RuntimeError("platform setup failed"),
        ),
        pytest.raises(RuntimeError, match="platform setup failed"),
    ):
        await async_setup_entry(hass, entry)
    coordinator.async_start.assert_not_awaited()
    coordinator.async_shutdown.assert_awaited_once()


async def test_unload_stops_tasks_without_revoking_account(hass, entry) -> None:
    """An ordinary reload can reconnect with the still-valid account link."""
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.api.async_disconnect = AsyncMock()
    entry.runtime_data = coordinator
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new_callable=AsyncMock,
        return_value=True,
    ):
        assert await async_unload_entry(hass, entry)
    coordinator.async_shutdown.assert_awaited_once()
    coordinator.api.async_disconnect.assert_not_awaited()


async def test_remove_entry_calls_ha_disconnect(hass, entry) -> None:
    """Deleting the account link explicitly calls the permanent REST endpoint."""
    api = MagicMock()
    api.async_disconnect = AsyncMock()
    with (
        patch(
            "custom_components.rhymax_mower.config_entry_oauth2_flow."
            "async_get_config_entry_implementation",
            new_callable=AsyncMock,
        ),
        patch("custom_components.rhymax_mower.RhymaxCloudApi", return_value=api),
    ):
        await async_remove_entry(hass, entry)
    api.async_disconnect.assert_awaited_once()


async def test_failed_platform_unload_keeps_coordinator_alive(hass, entry) -> None:
    """If HA retains the entities, their coordinator is not silently stopped."""
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    entry.runtime_data = coordinator
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new_callable=AsyncMock,
        return_value=False,
    ):
        assert not await async_unload_entry(hass, entry)
    coordinator.async_shutdown.assert_not_awaited()
