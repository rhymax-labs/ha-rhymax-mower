"""Shared mower state, push updates and independent REST reconciliation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    RhymaxApiError,
    RhymaxAuthError,
    RhymaxCloudApi,
    RhymaxDevice,
    RhymaxError,
    RhymaxMowerState,
    RhymaxNotAvailableError,
)
from .const import DOMAIN
from .websocket import RhymaxPushEvent, RhymaxWebSocket

_LOGGER = logging.getLogger(__name__)

type RhymaxConfigEntry = ConfigEntry[RhymaxCoordinator]


@dataclass(frozen=True, slots=True)
class RhymaxData:
    """Mowers on the account, by id, and the latest state of each."""

    devices: dict[str, RhymaxDevice]
    states: dict[str, RhymaxMowerState]


@dataclass(frozen=True, slots=True)
class RhymaxTimings:
    """Tunable fallback defaults, not a device-offline or latency guarantee."""

    watchdog_interval: float = 5
    push_stale_after: float = 90
    poll_interval: float = 30
    inventory_interval: float = 60
    request_min_interval: float = 5
    retry_max: float = 300
    command_confirmation_window: float = 30
    command_attempts: int = 3


@dataclass(slots=True)
class _Freshness:
    """Only accepted device state affects these clocks; heartbeats never do."""

    last_push_at: float | None = None
    last_rest_success_at: float | None = None
    revision: int = 0


@dataclass(slots=True)
class _RestBudget:
    """Budget shared by watchdog, manual and command confirmation reads."""

    last_attempt_at: float | None = None
    next_allowed_at: float = 0
    failures: int = 0

    def ready(self, now: float) -> bool:
        return now >= self.next_allowed_at

    def begin(self, now: float, minimum: float) -> None:
        # Charge before awaiting HTTP, including requests that fail or time out.
        self.last_attempt_at = now
        self.next_allowed_at = now + minimum

    def fail(self, now: float, timings: RhymaxTimings, err: RhymaxError) -> None:
        self.failures += 1
        delay = min(
            timings.poll_interval * 2 ** min(self.failures - 1, 10), timings.retry_max
        )
        if isinstance(err, RhymaxApiError) and err.retry_after is not None:
            delay = max(delay, err.retry_after)
        self.next_allowed_at = max(self.next_allowed_at, now + delay)


class RhymaxCoordinator(DataUpdateCoordinator[RhymaxData]):
    """Consume HA state pushes and reconcile each mower independently."""

    config_entry: RhymaxConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: RhymaxConfigEntry,
        api: RhymaxCloudApi,
        *,
        timings: RhymaxTimings = RhymaxTimings(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize state and clocks independent of the coordinator scheduler."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
        )
        self.api = api
        self._timings = timings
        self._clock = clock
        self._freshness: dict[str, _Freshness] = {}
        self._state_budgets: dict[str, _RestBudget] = {}
        self._inventory_budget = _RestBudget()
        self._inventory_success_at: float | None = None
        self._inventory_revision = 0
        self._inventory_requested = False
        self._inventory_task: asyncio.Task | None = None
        self._state_tasks: dict[str, asyncio.Task] = {}
        self._confirmation_tasks: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._running = False
        self._stopping = False
        self._auth_failed = False
        self._websocket = RhymaxWebSocket(
            api, self._handle_ws_event, self.async_auth_failed
        )

    def _snapshot(self) -> RhymaxData:
        return self.data if self.data is not None else RhymaxData({}, {})

    def _create_task(
        self, coroutine: Coroutine[Any, Any, Any], name: str
    ) -> asyncio.Task:
        task = self.config_entry.async_create_background_task(
            self.hass, coroutine, f"{DOMAIN}: {name}", eager_start=False
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def async_start(self) -> None:
        """Start once, after the platforms have consumed the REST cold start."""
        if self._running or self._stopping or self._auth_failed:
            return
        self._running = True
        self._create_task(self._websocket.async_run(), "websocket")
        self._create_task(self._async_watchdog(), "watchdog")

    async def async_shutdown(self) -> None:
        """Cancel sockets, retries, in-flight reads and confirmations on unload."""
        self._stopping = True
        self._running = False
        tasks = tuple(
            task for task in self._tasks if task is not asyncio.current_task()
        )
        for task in tasks:
            task.cancel()
        await self._websocket.async_stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().async_shutdown()

    @callback
    def async_auth_failed(self, err: RhymaxAuthError) -> None:
        """Stop account traffic after refresh failure and request re-linking once."""
        if self._stopping or self._auth_failed:
            return
        self._auth_failed = True
        self._running = False
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()
        self.async_set_update_error(
            ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            )
        )
        _LOGGER.warning("Rhymax account link needs renewal: %s", err)
        self.config_entry.async_start_reauth(self.hass)

    async def _async_update_data(self) -> RhymaxData:
        """REST cold start/manual reconciliation, using shared attempt budgets."""
        if self._auth_failed:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            )
        if self._stopping:
            return self._snapshot()
        try:
            if (
                inventory_task := self._request_inventory(raise_errors=True)
            ) is not None:
                await asyncio.shield(inventory_task)
            tasks = [
                task
                for mower_id in self._snapshot().devices
                if (task := self._request_state(mower_id, raise_auth=True)) is not None
            ]
            if tasks:
                await asyncio.gather(*(asyncio.shield(task) for task in tasks))
        except asyncio.CancelledError as err:
            # An auth failure in a shared background request cancels sibling
            # reads. Preserve that cause for DUC instead of reporting a normal
            # task cancellation or allowing a later refresh to mark success.
            if self._auth_failed and not self._stopping:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="auth_failed"
                ) from err
            raise
        except RhymaxAuthError as err:
            self.async_auth_failed(err)
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except RhymaxNotAvailableError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="linking_unavailable"
            ) from err
        except RhymaxError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        # A shared background read may already have reported an auth failure.
        if self._auth_failed:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            )
        # Return the current view, not a snapshot taken before awaiting REST.
        return self._snapshot()

    async def _async_watchdog(self) -> None:
        while self._running:
            await self._async_watchdog_tick()
            await asyncio.sleep(self._timings.watchdog_interval)

    async def _async_watchdog_tick(self) -> None:
        """Schedule due reads; a slow mower cannot block another mower's timer."""
        if self._stopping or self._auth_failed:
            return
        now = self._clock()
        if (
            self._inventory_task is None
            and self._inventory_budget.ready(now)
            and (
                self._inventory_requested
                or self._inventory_success_at is None
                or now - self._inventory_success_at >= self._timings.inventory_interval
            )
        ):
            self._request_inventory()

        stale_after = (
            self._timings.push_stale_after
            if self._websocket.connected
            else self._timings.poll_interval
        )
        for mower_id, freshness in tuple(self._freshness.items()):
            received = [
                stamp
                for stamp in (freshness.last_push_at, freshness.last_rest_success_at)
                if stamp is not None
            ]
            if not received or now - max(received) >= stale_after:
                self._request_state(mower_id)

    def _inventory_done(self, task: asyncio.Task) -> None:
        if self._inventory_task is task:
            self._inventory_task = None

    def _request_inventory(self, *, raise_errors: bool = False) -> asyncio.Task | None:
        if self._stopping or self._auth_failed:
            return None
        if self._inventory_task is not None:
            return self._inventory_task
        if not self._inventory_budget.ready(self._clock()):
            return None
        task = self._create_task(
            self._async_reconcile_inventory(raise_errors=raise_errors), "inventory"
        )
        self._inventory_task = task
        task.add_done_callback(self._inventory_done)
        return task

    async def _async_reconcile_inventory(self, *, raise_errors: bool = False) -> None:
        budget = self._inventory_budget
        if not budget.ready(self._clock()):
            return
        revision = self._inventory_revision
        budget.begin(self._clock(), self._timings.poll_interval)
        self._inventory_requested = False
        try:
            account = await self.api.async_get_account()
        except RhymaxError as err:
            budget.fail(self._clock(), self._timings, err)
            if raise_errors:
                raise
            if isinstance(err, RhymaxAuthError):
                self.async_auth_failed(err)
            else:
                _LOGGER.debug("Mower list reconciliation failed: %s", err)
            return
        if self._stopping or self._auth_failed:
            return
        budget.failures = 0
        self._inventory_success_at = self._clock()
        if revision == self._inventory_revision:
            self._apply_inventory({device.id: device for device in account.devices})

    def _apply_inventory(self, devices: dict[str, RhymaxDevice]) -> None:
        data = self._snapshot()
        if devices != data.devices:
            self._inventory_revision += 1
        for mower_id in data.devices.keys() - devices.keys():
            self._freshness.pop(mower_id, None)
            self._state_budgets.pop(mower_id, None)
            for tasks in (self._state_tasks, self._confirmation_tasks):
                if (task := tasks.pop(mower_id, None)) is not None:
                    task.cancel()
        for mower_id in devices:
            self._freshness.setdefault(mower_id, _Freshness())
            self._state_budgets.setdefault(mower_id, _RestBudget())
        self.async_set_updated_data(
            RhymaxData(
                devices,
                {key: value for key, value in data.states.items() if key in devices},
            )
        )

    def _request_state(
        self, mower_id: str, *, raise_auth: bool = False
    ) -> asyncio.Task | None:
        if self._stopping or self._auth_failed or mower_id not in self._freshness:
            return None
        if (task := self._state_tasks.get(mower_id)) is not None:
            return task
        if not self._state_budgets[mower_id].ready(self._clock()):
            return None
        task = self._create_task(
            self._async_fetch_state(mower_id, raise_auth), f"state {mower_id}"
        )
        self._state_tasks[mower_id] = task

        def done(completed: asyncio.Task) -> None:
            if self._state_tasks.get(mower_id) is completed:
                del self._state_tasks[mower_id]

        task.add_done_callback(done)
        return task

    async def _async_fetch_state(self, mower_id: str, raise_auth: bool) -> None:
        freshness = self._freshness[mower_id]
        revision = freshness.revision
        budget = self._state_budgets[mower_id]
        budget.begin(self._clock(), self._timings.request_min_interval)
        try:
            state = await self.api.async_get_state(mower_id)
        except RhymaxError as err:
            budget.fail(self._clock(), self._timings, err)
            if isinstance(err, RhymaxAuthError):
                if raise_auth:
                    raise
                self.async_auth_failed(err)
            else:
                _LOGGER.debug("State reconciliation failed for %s: %s", mower_id, err)
            return
        if (
            self._stopping
            or self._auth_failed
            or self._freshness.get(mower_id) is not freshness
        ):
            return
        budget.failures = 0
        freshness.last_rest_success_at = self._clock()
        # Presence changes may share last_activity_ts. A revision also protects
        # against a REST response finishing after a newer push during its await.
        current = self._snapshot().states.get(mower_id)
        if revision != freshness.revision or (
            current is not None
            and current.last_activity_ts is not None
            and state.last_activity_ts is not None
            and state.last_activity_ts < current.last_activity_ts
        ):
            return
        freshness.revision += 1
        data = self._snapshot()
        self.async_set_updated_data(
            RhymaxData(data.devices, {**data.states, mower_id: state})
        )

    @callback
    def _handle_ws_event(self, event: RhymaxPushEvent) -> None:
        """Apply validated, generation-filtered state; silence never means offline."""
        if self._stopping or self._auth_failed:
            return
        if event.type == "device_list_changed" and event.devices is not None:
            self._inventory_revision += 1
            self._apply_inventory({device.id: device for device in event.devices})
        elif (
            event.type == "device_state"
            and event.mac is not None
            and event.state is not None
        ):
            if (freshness := self._freshness.get(event.mac)) is None:
                # MVP omits inventory events; discover through a bounded list
                # request rather than inventing metadata for an unknown mower.
                self._inventory_requested = True
                return
            freshness.last_push_at = self._clock()
            freshness.revision += 1
            data = self._snapshot()
            self.async_set_updated_data(
                RhymaxData(data.devices, {**data.states, event.mac: event.state})
            )

    async def async_confirm_command(self, mower_id: str, command: str) -> None:
        """Schedule bounded state confirmation after REST accepted a command."""
        if self._stopping or self._auth_failed or mower_id not in self._freshness:
            return
        if (previous := self._confirmation_tasks.get(mower_id)) is not None:
            previous.cancel()
        task = self._create_task(
            self._async_confirm_command(mower_id, command), f"confirm {mower_id}"
        )
        self._confirmation_tasks[mower_id] = task

        def done(completed: asyncio.Task) -> None:
            if self._confirmation_tasks.get(mower_id) is completed:
                del self._confirmation_tasks[mower_id]

        task.add_done_callback(done)

    async def _async_confirm_command(self, mower_id: str, command: str) -> None:
        deadline = self._clock() + self._timings.command_confirmation_window
        revision = self._freshness[mower_id].revision
        for _ in range(self._timings.command_attempts):
            if self._freshness[mower_id].revision != revision and self._command_matches(
                mower_id, command
            ):
                return
            budget = self._state_budgets[mower_id]
            delay = max(0, budget.next_allowed_at - self._clock())
            remaining = deadline - self._clock()
            if remaining <= 0 or delay >= remaining:
                return
            if delay:
                await asyncio.sleep(delay)
            if self._freshness[mower_id].revision != revision and self._command_matches(
                mower_id, command
            ):
                return
            task = self._request_state(mower_id)
            if task is None:
                return
            try:
                async with asyncio.timeout(max(0, deadline - self._clock())):
                    # A replaced confirmation must not cancel a shared read.
                    await asyncio.shield(task)
            except TimeoutError:
                return
            if self._command_matches(mower_id, command):
                return

    def _command_matches(self, mower_id: str, command: str) -> bool:
        state = self._snapshot().states.get(mower_id)
        if state is None or not state.online:
            return False
        return state.activity in {
            "start": {"mowing"},
            "resume": {"mowing"},
            "pause": {"paused"},
            "stop": {"idle"},
            "dock": {"returning", "docked"},
        }.get(command, set())
