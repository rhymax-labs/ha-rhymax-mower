"""Real Home Assistant fixtures; only cloud I/O is mocked."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rhymax_mower.api import (
    RhymaxAccount,
    RhymaxCloudApi,
    RhymaxDevice,
    RhymaxMowerState,
)
from custom_components.rhymax_mower.const import DOMAIN
from custom_components.rhymax_mower.coordinator import RhymaxCoordinator

MOWER_A = "aabbccddeeff"
MOWER_B = "112233445566"
MOWER_C = "778899aabbcc"


@pytest.fixture(autouse=True)
def custom_integrations_enabled(enable_custom_integrations: None) -> None:
    """Allow loading this repository as a custom integration."""


@pytest.fixture
async def sandbox_loop_wakeup() -> AsyncGenerator[None]:
    """Keep executor callbacks moving when a sandbox denies local socket writes.

    Normal environments use asyncio's self-pipe unchanged. Restricted runners
    may permit socketpair creation but deny send(); bounded selector waits then
    provide wakeups without a network connection or replacing HA code.
    """
    loop = asyncio.get_running_loop()
    wake_socket = getattr(loop, "_csock", None)
    try:
        if wake_socket is not None:
            wake_socket.send(b"\0")
    except PermissionError:
        selector = loop._selector
        select = selector.select

        def select_with_wakeup(timeout=None):
            return select(0.005 if timeout is None else min(timeout, 0.005))

        # Keep the bounded wait for the loop's lifetime, including executor
        # shutdown after fixture teardown. No pending timer/task is created.
        selector.select = select_with_wakeup
    yield


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str, sandbox_loop_wakeup: None) -> str:
    """Keep all Home Assistant writes within pytest's workspace temp directory."""
    return hass_tmp_config_dir


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """An authorized OAuth entry registered with Home Assistant."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="RoboUP",
        unique_id="owner-account",
        data={
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "test-ha-access",
                "refresh_token": "test-ha-refresh",
                "expires_at": 4_000_000_000,
                "expires_in": 3600,
            },
        },
    )
    config_entry.add_to_hass(hass)
    config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return config_entry


@pytest.fixture
def devices() -> list[RhymaxDevice]:
    """Two independent owner mowers on one account."""
    return [
        RhymaxDevice(MOWER_A, "Front Yard", "RoboUP", "X9", "x9"),
        RhymaxDevice(MOWER_B, "Back Yard", "RoboUP", "X9", "x9"),
    ]


@pytest.fixture
def make_state() -> Callable[..., RhymaxMowerState]:
    """Construct a complete online state with explicit per-test overrides."""

    def _make_state(**changes: Any) -> RhymaxMowerState:
        return RhymaxMowerState(
            **{
                "id": MOWER_A,
                "online": True,
                "last_activity_ts": 1000,
                "activity": "docked",
                "task_state": 0,
                "sub_state": 0,
                "battery_level": 80,
                "is_docked": True,
                "is_charging": False,
                "progress_percent": 0,
                **changes,
            }
        )

    return _make_state


@pytest.fixture
def mock_api(
    devices: list[RhymaxDevice], make_state: Callable[..., RhymaxMowerState]
) -> AsyncMock:
    """Mock only the REST boundary, keeping coordinator and HA code real."""
    api = AsyncMock(spec=RhymaxCloudApi)
    api.async_get_account.return_value = RhymaxAccount("owner-account", devices)
    api.async_get_state.side_effect = lambda mower_id: make_state(id=mower_id)
    return api


@pytest.fixture
async def coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, mock_api: AsyncMock
) -> AsyncGenerator[RhymaxCoordinator]:
    """Cold-start real coordinator data without connecting to a real cloud."""
    result = RhymaxCoordinator(hass, entry, mock_api)
    await result.async_config_entry_first_refresh()
    entry.runtime_data = result
    yield result
    await result.async_shutdown()
