"""Entity state clearing and command acceptance behavior."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.exceptions import HomeAssistantError

from custom_components.rhymax_mower.api import (
    RhymaxApiError,
    RhymaxAuthError,
    parse_state,
)
from custom_components.rhymax_mower.binary_sensor import (
    RhymaxChargingSensor,
    RhymaxConnectivitySensor,
)
from custom_components.rhymax_mower.coordinator import RhymaxData
from custom_components.rhymax_mower.lawn_mower import RhymaxLawnMower
from custom_components.rhymax_mower.sensor import SENSORS, RhymaxSensor
from custom_components.rhymax_mower.websocket import RhymaxPushEvent

from .conftest import MOWER_A


async def test_offline_snapshot_clears_entity_values(coordinator) -> None:
    """An authoritative offline state clears every previously populated value."""
    mower = RhymaxLawnMower(coordinator, MOWER_A)
    battery = RhymaxSensor(coordinator, MOWER_A, SENSORS[0])
    progress = RhymaxSensor(coordinator, MOWER_A, SENSORS[1])
    charging = RhymaxChargingSensor(coordinator, MOWER_A)
    connectivity = RhymaxConnectivitySensor(coordinator, MOWER_A)
    assert mower.available
    assert battery.native_value == 80
    assert mower.activity == LawnMowerActivity.DOCKED

    coordinator._handle_ws_event(
        RhymaxPushEvent(
            type="device_state",
            subscription_generation=1,
            connection_epoch=1,
            event_id="offline-event",
            mac=MOWER_A,
            state=parse_state(
                {
                    "id": MOWER_A,
                    "online": False,
                    "last_activity_ts": 1234,
                    "activity": None,
                    "task_state": None,
                    "sub_state": None,
                    "battery_level": None,
                    "is_docked": None,
                    "is_charging": None,
                    "progress_percent": None,
                }
            ),
        )
    )

    assert not mower.available
    assert mower.activity is None
    assert battery.native_value is None
    assert progress.native_value is None
    assert charging.is_on is None
    assert not charging.available
    assert connectivity.available
    assert connectivity.is_on is False


@pytest.mark.parametrize(
    ("method", "command"),
    [
        ("async_start_mowing", "start"),
        ("async_pause", "pause"),
        ("async_dock", "dock"),
        ("async_stop", "stop"),
    ],
)
async def test_accepted_commands_schedule_targeted_confirmation(
    coordinator, mock_api, method, command
) -> None:
    """Acceptance starts bounded confirmation for only the commanded mower."""
    mower = RhymaxLawnMower(coordinator, MOWER_A)
    with (
        patch.object(
            coordinator, "async_confirm_command", new_callable=AsyncMock
        ) as confirm,
        patch.object(
            coordinator, "async_request_refresh", new_callable=AsyncMock
        ) as poll,
    ):
        await getattr(mower, method)()
    mock_api.async_send_command.assert_awaited_once_with(MOWER_A, command)
    confirm.assert_awaited_once_with(MOWER_A, command)
    poll.assert_not_awaited()


async def test_start_resumes_paused_mower(coordinator, mock_api) -> None:
    """The existing paused-to-resume mapping remains intact."""
    coordinator.async_set_updated_data(
        RhymaxData(
            coordinator.data.devices,
            {MOWER_A: replace(coordinator.data.states[MOWER_A], activity="paused")},
        )
    )
    with patch.object(coordinator, "async_confirm_command", new_callable=AsyncMock):
        await RhymaxLawnMower(coordinator, MOWER_A).async_start_mowing()
    mock_api.async_send_command.assert_awaited_once_with(MOWER_A, "resume")


@pytest.mark.parametrize(
    "error", [RhymaxApiError(409, "device_busy"), RhymaxAuthError("revoked")]
)
async def test_rejected_commands_do_not_start_confirmation(
    hass, coordinator, mock_api, entry, error
) -> None:
    """Only an accepted command consumes a confirmation budget."""
    mock_api.async_send_command.side_effect = error
    mower = RhymaxLawnMower(coordinator, MOWER_A)
    mower.hass = hass
    with (
        patch.object(
            coordinator, "async_confirm_command", new_callable=AsyncMock
        ) as confirm,
        patch.object(entry, "async_start_reauth") as reauth,
        pytest.raises(HomeAssistantError),
    ):
        await mower.async_dock()
    confirm.assert_not_awaited()
    assert reauth.called == isinstance(error, RhymaxAuthError)
