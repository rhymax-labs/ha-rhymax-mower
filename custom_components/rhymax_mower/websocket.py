"""Outbound HA-scoped push transport with shared OAuth and bounded recovery."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientWebSocketResponse, WSMsgType

from .api import (
    RhymaxApiError,
    RhymaxAuthError,
    RhymaxCloudApi,
    RhymaxDevice,
    RhymaxError,
    RhymaxMowerState,
    RhymaxSubscriptionError,
    parse_device,
    parse_state,
)
from .const import (
    WS_BACKOFF_MAX_SECONDS,
    WS_CLOSE_TIMEOUT_SECONDS,
    WS_EVENT_CACHE_SIZE,
    WS_TOKEN_CHECK_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RhymaxPushEvent:
    """Validated envelope; epoch is local and generation belongs to one socket."""

    type: str
    event_id: str
    subscription_generation: int
    connection_epoch: int
    mac: str | None = None
    state: RhymaxMowerState | None = None
    devices: tuple[RhymaxDevice, ...] | None = None


class RhymaxWebSocket:
    """One subscription per entry; no device presence inference from silence."""

    def __init__(
        self,
        api: RhymaxCloudApi,
        on_event: Callable[[RhymaxPushEvent], None],
        on_auth_error: Callable[[RhymaxAuthError], None],
    ) -> None:
        self._api = api
        self._on_event = on_event
        self._on_auth_error = on_auth_error
        self._stop = asyncio.Event()
        self._run_task: asyncio.Task | None = None
        self._socket: ClientWebSocketResponse | None = None
        self._connected = False
        self._last_message_at: float | None = None
        self._last_error: RhymaxError | None = None
        self._connection_epoch = 0
        self._generation = 0
        self._event_ids: OrderedDict[str, None] = OrderedDict()
        self._accepted_event = False

    @property
    def connected(self) -> bool:
        """Whether the current transport has completed its upgrade."""
        return self._connected

    @property
    def last_message_at(self) -> float | None:
        """Monotonic time of the last frame, independent of device freshness."""
        return self._last_message_at

    @property
    def last_error(self) -> RhymaxError | None:
        """Most recent transport error, including a terminal policy refusal."""
        return self._last_error

    @property
    def stopped(self) -> bool:
        """Whether recovery has stopped permanently."""
        return self._stop.is_set()

    async def async_run(self) -> None:
        """Run until unloaded or a permanent authentication/policy failure."""
        if self._run_task is not None or self._stop.is_set():
            return
        self._run_task = asyncio.current_task()
        attempts = 0
        rejected_token: str | None = None
        auth_retry_pending = False
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    if rejected_token is not None:
                        # Keep this fence through transient refresh failures.
                        # A rejected token must never reach ws_connect again.
                        token = await self._api.async_refresh_access_token(
                            rejected_token
                        )
                        if token == rejected_token:
                            raise RhymaxAuthError(
                                "The rejected push credential was not renewed"
                            )
                        rejected_token = None
                        auth_retry_pending = True
                    else:
                        token = await self._api.async_get_access_token()
                except RhymaxAuthError as err:
                    self._fail_auth(err)
                    return
                except RhymaxError as err:
                    self._last_error = err
                    await self._wait_to_retry(attempts)
                    attempts += 1
                    continue

                try:
                    try:
                        self._socket = await self._api.async_connect_websocket(token)
                    except RhymaxAuthError:
                        close_code = 4401
                    else:
                        self._connection_epoch += 1
                        self._generation = 0
                        self._event_ids.clear()
                        self._accepted_event = False
                        self._connected = True
                        self._last_error = None
                        close_code = await self._serve(token, self._connection_epoch)
                        if self._accepted_event:
                            auth_retry_pending = False
                        if close_code is None and not self._stop.is_set():
                            # The shared REST OAuth token changed. Reconnect
                            # promptly with that token, after closing this one.
                            continue
                except RhymaxAuthError as err:
                    self._fail_auth(err)
                    return
                except RhymaxSubscriptionError as err:
                    self._last_error = err
                    self._stop.set()
                    _LOGGER.error("%s", err)
                    return
                except (RhymaxError, ClientError, TimeoutError) as err:
                    self._last_error = (
                        err if isinstance(err, RhymaxError) else RhymaxError(str(err))
                    )
                    close_code = 1006
                finally:
                    self._connected = False
                    await self._close_socket()

                if self._stop.is_set():
                    return
                if close_code in (4401, 1008):
                    if auth_retry_pending:
                        self._fail_auth(
                            RhymaxAuthError(
                                "Rhymax cloud rejected the renewed push account link"
                            )
                        )
                        return
                    rejected_token = token
                    continue
                if close_code == 4403:
                    self._last_error = RhymaxSubscriptionError(
                        "Rhymax cloud refused the push subscription (4403)"
                    )
                    self._stop.set()
                    _LOGGER.error("%s", self._last_error)
                    return
                if time.monotonic() - started >= WS_BACKOFF_MAX_SECONDS:
                    attempts = 0
                await self._wait_to_retry(attempts)
                attempts += 1
        finally:
            self._connected = False
            await self._close_socket()
            self._run_task = None

    async def async_stop(self) -> None:
        """Stop socket, recovery delay and child tasks with bounded cleanup."""
        self._stop.set()
        task = self._run_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                async with asyncio.timeout(WS_CLOSE_TIMEOUT_SECONDS + 1):
                    await task
            except (asyncio.CancelledError, TimeoutError):
                pass
        await self._close_socket()

    async def _serve(self, token: str, epoch: int) -> int | None:
        listener = asyncio.create_task(self._listen(epoch))
        monitor = asyncio.create_task(self._watch_token(token))
        try:
            done, _ = await asyncio.wait(
                {listener, monitor}, return_when=asyncio.FIRST_COMPLETED
            )
            if listener in done:
                return listener.result()
            monitor.result()  # Propagate permanent refresh failures.
            return None
        finally:
            listener.cancel()
            monitor.cancel()
            await asyncio.gather(listener, monitor, return_exceptions=True)

    async def _watch_token(self, token: str) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(WS_TOKEN_CHECK_SECONDS)
            if await self._api.async_get_access_token() != token:
                return

    async def _listen(self, epoch: int) -> int:
        socket = self._socket
        assert socket is not None
        while not self._stop.is_set():
            message = await socket.receive()
            if epoch != self._connection_epoch:
                return 1000
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                return socket.close_code or (
                    message.data if message.type == WSMsgType.CLOSE else 1006
                )
            self._last_message_at = time.monotonic()
            if message.type == WSMsgType.PING:
                await socket.pong(message.data)
            elif message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except (TypeError, ValueError):
                    _LOGGER.debug("Ignoring malformed Rhymax push JSON")
                    continue
                self._dispatch(payload, epoch)
        return 1000

    def _dispatch(self, payload: Any, epoch: int) -> None:
        if epoch != self._connection_epoch or self._stop.is_set():
            return
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type")
        # Some servers expose a minimal application heartbeat. It only proves
        # channel liveness and never changes per-device state or freshness.
        if event_type == "ping" and "event_id" not in payload:
            return
        event_id = payload.get("event_id")
        generation = payload.get("subscription_generation")
        if (
            not isinstance(event_type, str)
            or event_type not in {"device_state", "device_list_changed", "ping"}
            or not isinstance(event_id, str)
            or not 1 <= len(event_id) <= 256
            or type(generation) is not int
            or generation < 1
            or generation < self._generation
            or event_id in self._event_ids
        ):
            return
        try:
            if event_type == "device_state":
                mac = payload.get("mac")
                if not isinstance(mac, str):
                    return
                state = parse_state(payload.get("state"), expected_id=mac)
                event = RhymaxPushEvent(
                    event_type, event_id, generation, epoch, mac, state
                )
            elif event_type == "device_list_changed":
                devices = payload.get("devices")
                if not isinstance(devices, list):
                    return
                parsed = tuple(parse_device(device) for device in devices)
                if len({device.id for device in parsed}) != len(parsed):
                    return
                event = RhymaxPushEvent(
                    event_type, event_id, generation, epoch, devices=parsed
                )
            else:
                event = RhymaxPushEvent(event_type, event_id, generation, epoch)
        except RhymaxApiError:
            _LOGGER.debug("Ignoring invalid Rhymax push projection")
            return
        self._generation = generation
        self._event_ids[event_id] = None
        if len(self._event_ids) > WS_EVENT_CACHE_SIZE:
            self._event_ids.popitem(last=False)
        self._accepted_event = True
        self._on_event(event)

    async def _close_socket(self) -> None:
        socket, self._socket = self._socket, None
        if socket is None:
            return
        try:
            async with asyncio.timeout(WS_CLOSE_TIMEOUT_SECONDS):
                await socket.close()
        except (ClientError, TimeoutError):
            _LOGGER.debug("Rhymax push socket did not close cleanly")

    async def _wait_to_retry(self, attempt: int) -> None:
        delay = min(
            WS_BACKOFF_MAX_SECONDS,
            random.uniform(0.8, 1.2) * (2 ** min(attempt, 6)),
        )
        try:
            async with asyncio.timeout(delay):
                await self._stop.wait()
        except TimeoutError:
            pass

    def _fail_auth(self, error: RhymaxAuthError) -> None:
        self._last_error = error
        self._stop.set()
        self._on_auth_error(error)
