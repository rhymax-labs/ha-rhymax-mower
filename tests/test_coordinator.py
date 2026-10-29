"""Watchdog, ordering, and bounded REST reconciliation under a real HA loop."""

import asyncio
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.rhymax_mower.api import (
    RhymaxAccount,
    RhymaxApiError,
    RhymaxAuthError,
    RhymaxConnectionError,
    RhymaxDevice,
)
from custom_components.rhymax_mower.coordinator import (
    RhymaxCoordinator,
    RhymaxTimings,
)
from custom_components.rhymax_mower.websocket import RhymaxPushEvent

from .conftest import MOWER_A, MOWER_B, MOWER_C


@dataclass
class Clock:
    """Advance only the integration's monotonic deadlines, never HA timers."""

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def timed_coordinator(hass, entry, mock_api, clock):
    coordinator = RhymaxCoordinator(hass, entry, mock_api, clock=clock)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator._websocket._connected = True
    mock_api.reset_mock()
    yield coordinator
    await coordinator.async_shutdown()


def push(coordinator, state, event_id="state-event"):
    """Deliver a transport-validated device snapshot."""
    coordinator._handle_ws_event(
        RhymaxPushEvent(
            type="device_state",
            event_id=event_id,
            subscription_generation=1,
            connection_epoch=1,
            mac=state.id,
            state=state,
        )
    )


async def tick(hass, coordinator):
    await coordinator._async_watchdog_tick()
    tasks = list(coordinator._state_tasks.values())
    if coordinator._inventory_task is not None:
        tasks.append(coordinator._inventory_task)
    if tasks:
        await asyncio.gather(*tasks)
    await hass.async_block_till_done()


async def test_active_mower_does_not_starve_silent_mower(
    hass, timed_coordinator, mock_api, make_state, clock
):
    """Only the silent device needs state reconciliation despite repeated pushes."""
    coordinator = timed_coordinator
    for elapsed in (25, 50, 75, 100):
        clock.now = 1000 + elapsed
        push(coordinator, make_state(id=MOWER_A, last_activity_ts=elapsed + 1000))
        await tick(hass, coordinator)

    mock_api.async_get_state.assert_awaited_once_with(MOWER_B)
    assert coordinator.data.states[MOWER_B].online is True
    assert coordinator.data.states[MOWER_B].activity == "docked"
    assert coordinator._freshness[MOWER_A].last_push_at == clock.now
    assert coordinator._freshness[MOWER_B].last_push_at is None
    assert coordinator._freshness[MOWER_B].last_rest_success_at == clock.now


async def test_watchdog_task_keeps_running_during_continuous_push(
    timed_coordinator, mock_api, make_state, clock
):
    """Drive the real watchdog task, without manually invoking any tick."""
    coordinator = timed_coordinator
    coordinator._timings = replace(coordinator._timings, watchdog_interval=0.001)
    with patch.object(coordinator._websocket, "async_run", new_callable=AsyncMock):
        await coordinator.async_start()
        for _ in range(12):
            clock.now += 10
            push(coordinator, make_state(id=MOWER_A, last_activity_ts=int(clock.now)))
            await asyncio.sleep(0.003)
        await coordinator.async_shutdown()

    mock_api.async_get_state.assert_awaited_once_with(MOWER_B)
    assert mock_api.async_get_account.await_count >= 1
    assert coordinator.data.states[MOWER_B].online


async def test_heartbeat_does_not_refresh_any_device(
    hass, timed_coordinator, mock_api, clock
):
    """A live channel cannot hide a missing device-state stream."""
    coordinator = timed_coordinator
    clock.now += 100
    coordinator._websocket._last_message_at = clock.now
    coordinator._handle_ws_event(RhymaxPushEvent("ping", "heartbeat", 1, 1))
    await tick(hass, coordinator)

    assert {call.args[0] for call in mock_api.async_get_state.await_args_list} == {
        MOWER_A,
        MOWER_B,
    }
    assert all(item.last_push_at is None for item in coordinator._freshness.values())
    assert all(state.online for state in coordinator.data.states.values())


async def test_disconnected_transport_uses_normal_rest_cadence(
    hass, timed_coordinator, mock_api, clock
):
    """REST continues on both mowers when the push channel is unavailable."""
    timed_coordinator._websocket._connected = False
    clock.now += 29
    await tick(hass, timed_coordinator)
    mock_api.async_get_state.assert_not_awaited()
    clock.now += 2
    await tick(hass, timed_coordinator)
    assert mock_api.async_get_state.await_count == 2


@pytest.mark.parametrize(
    "error",
    [
        RhymaxConnectionError("upstream unavailable"),
        RhymaxApiError(429, "rate_limited", retry_after=180),
    ],
)
async def test_failed_attempts_are_throttled_and_do_not_mark_offline(
    hass, timed_coordinator, mock_api, make_state, clock, error
):
    """Failed requests consume retry budget even though no fresh state arrived."""
    coordinator = timed_coordinator
    clock.now += 100
    push(coordinator, make_state(id=MOWER_A))
    mock_api.async_get_state.side_effect = error
    await tick(hass, coordinator)
    assert coordinator._state_budgets[MOWER_B].last_attempt_at == clock.now
    assert coordinator._freshness[MOWER_B].last_rest_success_at == 1000

    for _ in range(5):
        clock.now += 1
        await tick(hass, coordinator)
    mock_api.async_get_state.assert_awaited_once_with(MOWER_B)
    assert coordinator.data.states[MOWER_B].online is True
    if isinstance(error, RhymaxApiError):
        assert coordinator._state_budgets[MOWER_B].next_allowed_at >= 1280

    clock.now = coordinator._state_budgets[MOWER_B].next_allowed_at
    push(coordinator, make_state(id=MOWER_A))
    await tick(hass, coordinator)
    assert mock_api.async_get_state.await_count == 2
    assert coordinator._state_budgets[MOWER_B].failures == 2
    assert coordinator._state_budgets[MOWER_B].next_allowed_at >= clock.now + 60


async def test_inflight_rest_cannot_overwrite_newer_push(
    hass, timed_coordinator, mock_api, make_state, clock
):
    """A response that began before a push is discarded when it finishes later."""
    coordinator = timed_coordinator
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_state(mower_id):
        started.set()
        await release.wait()
        return make_state(id=mower_id, battery_level=5, last_activity_ts=1100)

    clock.now += 100
    push(coordinator, make_state(id=MOWER_A))
    mock_api.async_get_state.side_effect = delayed_state
    await coordinator._async_watchdog_tick()
    await asyncio.wait_for(started.wait(), 1)
    pending_state = coordinator._state_tasks[MOWER_B]
    push(coordinator, make_state(id=MOWER_B, battery_level=92, last_activity_ts=1200))
    release.set()
    await pending_state
    await hass.async_block_till_done()
    assert coordinator.data.states[MOWER_B].battery_level == 92
    assert coordinator.data.states[MOWER_B].last_activity_ts == 1200


async def test_older_rest_snapshot_does_not_regress_state(
    hass, timed_coordinator, mock_api, make_state, clock
):
    """A successful HTTP response is not proof that its telemetry is newer."""
    clock.now += 100
    push(timed_coordinator, make_state(id=MOWER_A))
    mock_api.async_get_state.side_effect = lambda mower_id: make_state(
        id=mower_id, battery_level=5, last_activity_ts=900
    )
    await tick(hass, timed_coordinator)
    assert timed_coordinator.data.states[MOWER_B].battery_level == 80


async def test_inventory_reconciles_while_mowers_keep_pushing(
    hass, timed_coordinator, mock_api, make_state, devices, clock
):
    """Independent REST list reconciliation discovers an added and removed mower."""
    new_device = RhymaxDevice(MOWER_C, "Side Yard", "RoboUP", "X9", "x9")
    mock_api.async_get_account.return_value = RhymaxAccount(
        "owner-account", [devices[0], new_device]
    )
    clock.now += 61
    push(timed_coordinator, make_state(id=MOWER_A))
    push(timed_coordinator, make_state(id=MOWER_B))
    await tick(hass, timed_coordinator)
    assert set(timed_coordinator.data.devices) == {MOWER_A, MOWER_C}
    assert MOWER_B not in timed_coordinator.data.states
    assert MOWER_B not in timed_coordinator._freshness
    mock_api.async_get_account.assert_awaited_once()


async def test_new_inventory_push_wins_over_inflight_rest_list(
    hass, timed_coordinator, mock_api, devices, clock
):
    """An old list response cannot reintroduce an already unbound mower."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def old_list():
        started.set()
        await release.wait()
        return RhymaxAccount("owner-account", devices)

    mock_api.async_get_account.side_effect = old_list
    clock.now += 61
    await timed_coordinator._async_watchdog_tick()
    await asyncio.wait_for(started.wait(), 1)
    pending_inventory = timed_coordinator._inventory_task
    timed_coordinator._handle_ws_event(
        RhymaxPushEvent("device_list_changed", "new-list", 1, 1, devices=(devices[0],))
    )
    release.set()
    await pending_inventory
    await hass.async_block_till_done()
    assert set(timed_coordinator.data.devices) == {MOWER_A}


async def test_removed_then_readded_device_rejects_old_inflight_response(
    hass, timed_coordinator, mock_api, make_state, devices, clock
):
    """Rebinding the same MAC cannot revive state from its previous membership."""
    coordinator = timed_coordinator
    started = asyncio.Event()
    release = asyncio.Event()

    async def late_response(mower_id):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Exercise the revision/identity fence even if a transport returns
            # a completed response at the same time as cancellation.
            await release.wait()
        return make_state(id=mower_id, battery_level=1, last_activity_ts=1100)

    clock.now += 100
    push(coordinator, make_state(id=MOWER_A))
    mock_api.async_get_state.side_effect = late_response
    await coordinator._async_watchdog_tick()
    await asyncio.wait_for(started.wait(), 1)
    previous_request = coordinator._state_tasks[MOWER_B]
    previous_freshness = coordinator._freshness[MOWER_B]
    coordinator._handle_ws_event(
        RhymaxPushEvent("device_list_changed", "removed", 1, 1, devices=(devices[0],))
    )
    coordinator._handle_ws_event(
        RhymaxPushEvent("device_list_changed", "readded", 1, 1, devices=tuple(devices))
    )
    push(coordinator, make_state(id=MOWER_B, battery_level=95, last_activity_ts=1200))
    release.set()
    await previous_request
    await hass.async_block_till_done()
    assert coordinator._freshness[MOWER_B] is not previous_freshness
    assert coordinator.data.states[MOWER_B].battery_level == 95


async def test_auth_failure_requests_reauth_once_without_falsifying_presence(
    hass, entry, timed_coordinator, mock_api, make_state, clock
):
    """A terminal account failure disables updates while retaining known presence."""
    coordinator = timed_coordinator
    with patch.object(entry, "async_start_reauth") as reauth:
        coordinator.async_auth_failed(RhymaxAuthError("revoked"))
        coordinator.async_auth_failed(RhymaxAuthError("revoked again"))
        reauth.assert_called_once_with(hass)
        assert not coordinator.last_update_success
        assert coordinator.data.states[MOWER_A].online
        clock.now += 1000
        push(coordinator, make_state(id=MOWER_A, online=False))
        await coordinator._async_watchdog_tick()
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
    assert not coordinator.last_update_success
    assert coordinator.data.states[MOWER_A].online
    mock_api.async_get_account.assert_not_awaited()
    mock_api.async_get_state.assert_not_awaited()


async def test_manual_state_auth_failure_stops_channel_and_sibling_read(
    hass, entry, timed_coordinator, mock_api, clock
):
    """A manual read that detects revoked auth stops all account traffic."""
    coordinator = timed_coordinator
    channel_started = asyncio.Event()
    sibling_started = asyncio.Event()
    pending = asyncio.Event()

    async def channel():
        channel_started.set()
        await pending.wait()

    async def state(mower_id):
        if mower_id == MOWER_A:
            await sibling_started.wait()
            raise RhymaxAuthError("refresh token revoked")
        sibling_started.set()
        await pending.wait()

    with (
        patch.object(coordinator._websocket, "async_run", side_effect=channel),
        patch.object(entry, "async_start_reauth") as reauth,
    ):
        await coordinator.async_start()
        await asyncio.wait_for(channel_started.wait(), 1)
        channel_tasks = tuple(coordinator._tasks)
        clock.now += 10
        mock_api.async_get_state.side_effect = state
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        await asyncio.gather(*tuple(coordinator._tasks), return_exceptions=True)
        assert all(task.cancelled() for task in channel_tasks)
        assert not coordinator._state_tasks
        assert not coordinator._running
        assert not coordinator.last_update_success
        assert coordinator.data.states[MOWER_A].online
        reauth.assert_called_once_with(hass)


@pytest.mark.parametrize("with_sibling", [False, True])
async def test_manual_refresh_sharing_background_auth_failure_stays_failed(
    hass, entry, timed_coordinator, mock_api, clock, with_sibling
):
    """A swallowed background auth error cannot make its manual waiter succeed."""
    coordinator = timed_coordinator
    started = {MOWER_A: asyncio.Event(), MOWER_B: asyncio.Event()}
    release = asyncio.Event()
    pending = asyncio.Event()

    async def state(mower_id):
        started[mower_id].set()
        if mower_id == MOWER_A:
            await release.wait()
            raise RhymaxAuthError("shared request rejected")
        await pending.wait()

    clock.now += 100
    if not with_sibling:
        coordinator._state_budgets[MOWER_B].next_allowed_at = clock.now + 1000
    mock_api.async_get_state.side_effect = state
    with patch.object(entry, "async_start_reauth") as reauth:
        await coordinator._async_watchdog_tick()
        await asyncio.wait_for(started[MOWER_A].wait(), 1)
        if with_sibling:
            await asyncio.wait_for(started[MOWER_B].wait(), 1)
        manual = asyncio.create_task(coordinator._async_update_data())
        await asyncio.sleep(0)
        release.set()
        result = (await asyncio.gather(manual, return_exceptions=True))[0]
        await asyncio.gather(*tuple(coordinator._tasks), return_exceptions=True)
        assert isinstance(result, ConfigEntryAuthFailed)
        assert not coordinator.last_update_success
        assert coordinator.data.states[MOWER_A].online
        reauth.assert_called_once_with(hass)


async def test_manual_inventory_and_watchdog_share_slow_request(
    hass, timed_coordinator, mock_api, devices, clock
):
    """Crossing multiple inventory deadlines never duplicates a pending list read."""
    coordinator = timed_coordinator
    started = asyncio.Event()
    release = asyncio.Event()

    async def inventory():
        started.set()
        await release.wait()
        return RhymaxAccount("owner-account", devices)

    clock.now += 31
    mock_api.async_get_account.side_effect = inventory
    manual = asyncio.create_task(coordinator._async_update_data())
    try:
        await asyncio.wait_for(started.wait(), 1)
        inventory_task = coordinator._inventory_task
        for _ in range(3):
            clock.now += 65
            await coordinator._async_watchdog_tick()
            await asyncio.sleep(0)
            assert coordinator._inventory_task is inventory_task
        mock_api.async_get_account.assert_awaited_once()
        release.set()
        await manual
        assert not coordinator._inventory_task
        assert set(coordinator.data.devices) == {MOWER_A, MOWER_B}
    finally:
        release.set()
        if not manual.done():
            manual.cancel()
        await asyncio.gather(manual, return_exceptions=True)


async def test_command_confirmation_is_finite_and_targeted(hass, entry, mock_api):
    """A command that never converges stops its own finite confirmation loop."""
    coordinator = RhymaxCoordinator(
        hass,
        entry,
        mock_api,
        timings=RhymaxTimings(
            request_min_interval=0.001,
            command_confirmation_window=0.04,
            command_attempts=3,
        ),
    )
    await coordinator.async_config_entry_first_refresh()
    mock_api.reset_mock()
    try:
        await coordinator.async_confirm_command(MOWER_A, "start")
        await asyncio.sleep(0.06)
        await hass.async_block_till_done()
        assert 1 <= mock_api.async_get_state.await_count <= 3
        assert all(
            call.args == (MOWER_A,) for call in mock_api.async_get_state.await_args_list
        )
        assert not coordinator._confirmation_tasks
        count = mock_api.async_get_state.await_count
        await asyncio.sleep(0.02)
        assert mock_api.async_get_state.await_count == count
    finally:
        await coordinator.async_shutdown()


async def test_converged_push_stops_pending_command_confirmation(
    hass, entry, mock_api, make_state
):
    """A matching push satisfies confirmation before the next allowed REST read."""
    coordinator = RhymaxCoordinator(
        hass,
        entry,
        mock_api,
        timings=RhymaxTimings(
            request_min_interval=0.02,
            command_confirmation_window=0.1,
        ),
    )
    await coordinator.async_config_entry_first_refresh()
    mock_api.reset_mock()
    try:
        await coordinator.async_confirm_command(MOWER_A, "start")
        await asyncio.sleep(0)
        push(
            coordinator,
            make_state(id=MOWER_A, activity="mowing", last_activity_ts=2000),
        )
        await asyncio.sleep(0.04)
        mock_api.async_get_state.assert_not_awaited()
        assert not coordinator._confirmation_tasks
    finally:
        await coordinator.async_shutdown()


async def test_shutdown_cancels_watchdog_ws_and_inflight_requests(
    hass, timed_coordinator, mock_api, clock
):
    """Unload drains every owned task and prevents late updates or retries."""
    coordinator = timed_coordinator
    pending = asyncio.Event()
    started = asyncio.Event()

    async def never_returns(mower_id):
        started.set()
        await pending.wait()

    mock_api.async_get_state.side_effect = never_returns
    with patch.object(coordinator._websocket, "async_run", new_callable=AsyncMock):
        await coordinator.async_start()
        clock.now += 100
        await coordinator._async_watchdog_tick()
        await asyncio.wait_for(started.wait(), 1)
        tasks = list(coordinator._state_tasks.values())
        await coordinator.async_confirm_command(MOWER_A, "start")
        await coordinator.async_shutdown()
    assert all(task.done() for task in tasks)
    assert not coordinator._state_tasks
    assert not coordinator._confirmation_tasks
    count = mock_api.async_get_state.await_count
    clock.now += 1000
    await coordinator._async_watchdog_tick()
    assert mock_api.async_get_state.await_count == count
