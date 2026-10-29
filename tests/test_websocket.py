"""Push envelope, reconnect/auth fencing and bounded transport lifecycle."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import WSMessage, WSMsgType

from custom_components.rhymax_mower.api import (
    RhymaxAuthError,
    RhymaxCloudApi,
    RhymaxConnectionError,
    RhymaxSubscriptionError,
)
from custom_components.rhymax_mower.websocket import RhymaxWebSocket

MAC = "aabbccddeeff"


def envelope(event_id="opaque-uuid", generation=1, **changes):
    return {
        "type": "device_state",
        "event_id": event_id,
        "subscription_generation": generation,
        "mac": MAC,
        "state": {"id": MAC, "online": True, "activity": "docked"},
        **changes,
    }


class Socket:
    def __init__(self, *messages):
        self.messages = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message)
        self.close_code = None
        self.close = AsyncMock()
        self.pong = AsyncMock()

    async def receive(self):
        message = await self.messages.get()
        if isinstance(message, dict):
            return WSMessage(WSMsgType.TEXT, json.dumps(message), "")
        if isinstance(message, int):
            self.close_code = message
            return WSMessage(WSMsgType.CLOSE, message, "")
        return message


def client_and_api(*sockets):
    api = AsyncMock(spec=RhymaxCloudApi)
    api.async_get_access_token.return_value = "old-token"
    api.async_refresh_access_token.return_value = "new-token"
    api.async_connect_websocket.side_effect = sockets
    on_event, on_auth_error = MagicMock(), MagicMock()
    client = RhymaxWebSocket(api, on_event, on_auth_error)
    return client, api, on_event, on_auth_error


async def test_frozen_snapshot_ping_offline_and_invalid_data():
    socket = Socket(
        envelope("first"),
        WSMessage(WSMsgType.PING, b"heartbeat", ""),
        {"type": "ping"},
        envelope("broken", state={"id": MAC, "online": "false"}),
        envelope("offline", state={"id": MAC, "online": False, "battery_level": 95}),
        4403,
    )
    client, _, on_event, on_auth_error = client_and_api(socket)
    await client.async_run()
    assert on_event.call_count == 2
    assert on_event.call_args_list[0].args[0].state.activity == "docked"
    offline = on_event.call_args_list[1].args[0].state
    assert offline.online is False
    assert offline.battery_level is None
    socket.pong.assert_awaited_once_with(b"heartbeat")
    assert client.last_message_at is not None
    assert not client.connected
    assert client.stopped
    assert isinstance(client.last_error, RhymaxSubscriptionError)
    on_auth_error.assert_not_called()


def test_epoch_generation_and_opaque_equality_dedup():
    client, _, on_event, _ = client_and_api()
    client._connection_epoch = 4
    client._dispatch(envelope("z", 2), 4)
    client._dispatch(envelope("a", 2), 4)  # Lexically older remains valid.
    client._dispatch(envelope("a", 3), 4)  # Equality duplicate.
    client._dispatch(envelope("old-generation", 1), 4)
    client._dispatch(envelope("old-connection", 10), 3)
    client._dispatch(envelope("new-generation", 3), 4)
    assert [call.args[0].event_id for call in on_event.call_args_list] == [
        "z",
        "a",
        "new-generation",
    ]
    assert on_event.call_args.args[0].connection_epoch == 4


def test_dedup_cache_bounded_and_invalid_frame_does_not_advance_generation():
    client, _, on_event, _ = client_and_api()
    client._connection_epoch = 1
    with patch("custom_components.rhymax_mower.websocket.WS_EVENT_CACHE_SIZE", 3):
        for index in range(6):
            client._dispatch(envelope(str(index)), 1)
        assert len(client._event_ids) == 3
    client._dispatch(envelope("invalid", 10, state={}), 1)
    client._dispatch(envelope("valid", 2), 1)
    client._dispatch({"type": []}, 1)
    assert on_event.call_args.args[0].event_id == "valid"


def test_inventory_envelope_validates_full_list_and_mac_identity():
    client, _, on_event, _ = client_and_api()
    client._connection_epoch = 1
    client._dispatch(envelope("mismatch", mac="112233445566"), 1)
    client._dispatch(envelope("bad-list", type="device_list_changed", devices=[{}]), 1)
    client._dispatch(
        envelope(
            "list",
            type="device_list_changed",
            devices=[
                {
                    "id": MAC,
                    "name": "Front Yard",
                    "model": "X9",
                    "product": "x9",
                }
            ],
        ),
        1,
    )
    assert on_event.call_count == 1
    assert on_event.call_args.args[0].devices[0].id == MAC


@pytest.mark.parametrize("code", [4401, 1008])
async def test_auth_close_refreshes_once_then_uses_new_token(code):
    first, second = Socket(code), Socket(envelope(), 4403)
    client, api, on_event, on_auth_error = client_and_api(first, second)
    await client.async_run()
    api.async_refresh_access_token.assert_awaited_once_with("old-token")
    assert [call.args[0] for call in api.async_connect_websocket.call_args_list] == [
        "old-token",
        "new-token",
    ]
    assert on_event.call_args.args[0].connection_epoch == 2
    on_auth_error.assert_not_called()
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()


@pytest.mark.parametrize("code", [4401, 1008])
async def test_refreshed_credential_rejected_stops_and_requests_reauth(code):
    client, api, _, on_auth_error = client_and_api(Socket(code), Socket(code))
    await client.async_run()
    assert api.async_connect_websocket.await_count == 2
    api.async_refresh_access_token.assert_awaited_once()
    on_auth_error.assert_called_once()
    assert client.stopped


async def test_refresh_failure_or_unchanged_token_stops_without_reconnect():
    for result in (RhymaxAuthError("invalid grant"), "old-token"):
        client, api, _, on_auth_error = client_and_api(Socket(4401))
        if isinstance(result, Exception):
            api.async_refresh_access_token.side_effect = result
        else:
            api.async_refresh_access_token.return_value = result
        await client.async_run()
        assert api.async_connect_websocket.await_count == 1
        on_auth_error.assert_called_once()


async def test_transient_refresh_failure_retries_refresh_not_rejected_socket():
    client, api, _, on_auth_error = client_and_api(Socket(4401), Socket(4403))
    api.async_refresh_access_token.side_effect = [
        RhymaxConnectionError("temporary"),
        "renewed",
    ]
    client._wait_to_retry = AsyncMock()
    await client.async_run()
    assert [call.args[0] for call in api.async_connect_websocket.call_args_list] == [
        "old-token",
        "renewed",
    ]
    assert api.async_refresh_access_token.await_count == 2
    client._wait_to_retry.assert_awaited_once_with(0)
    on_auth_error.assert_not_called()


async def test_4502_and_network_failure_use_backoff():
    client, api, _, _ = client_and_api(
        Socket(4502),
        RhymaxConnectionError("unreachable"),
        Socket(4403),
    )
    client._wait_to_retry = AsyncMock()
    await client.async_run()
    assert [call.args[0] for call in client._wait_to_retry.call_args_list] == [0, 1]
    assert api.async_connect_websocket.await_count == 3


async def test_backoff_exponential_capped_and_interruptible():
    client, _, _, _ = client_and_api()
    client._stop.wait = AsyncMock(side_effect=TimeoutError)
    with (
        patch(
            "custom_components.rhymax_mower.websocket.random.uniform", return_value=1
        ),
        patch(
            "custom_components.rhymax_mower.websocket.asyncio.timeout",
            wraps=asyncio.timeout,
        ) as timeout,
    ):
        for attempt in range(10):
            await client._wait_to_retry(attempt)
    assert [call.args[0] for call in timeout.call_args_list] == [
        1,
        2,
        4,
        8,
        16,
        32,
        60,
        60,
        60,
        60,
    ]


async def test_external_rest_token_rotation_closes_old_socket(monkeypatch):
    monkeypatch.setattr(
        "custom_components.rhymax_mower.websocket.WS_TOKEN_CHECK_SECONDS", 0
    )
    first, second = Socket(), Socket(4403)
    client, api, _, _ = client_and_api(first, second)
    api.async_get_access_token.side_effect = [
        "old-token",
        "rotated-by-rest",
        "rotated-by-rest",
    ]
    await asyncio.wait_for(client.async_run(), 1)
    assert [call.args[0] for call in api.async_connect_websocket.call_args_list] == [
        "old-token",
        "rotated-by-rest",
    ]
    first.close.assert_awaited_once()


async def test_unload_cancels_handshake_and_prevents_reconnect():
    client, api, _, _ = client_and_api()
    entered = asyncio.Event()

    async def blocked_connect(token):
        entered.set()
        await asyncio.Event().wait()

    api.async_connect_websocket.side_effect = blocked_connect
    task = asyncio.create_task(client.async_run())
    await entered.wait()
    await client.async_stop()
    assert task.done()
    assert not client.connected
    assert client.stopped
    await client.async_run()
    assert api.async_connect_websocket.await_count == 1


async def test_unload_cancels_listener_and_token_monitor():
    client, api, _, _ = client_and_api(Socket())
    connected = asyncio.Event()
    socket = Socket()

    async def connect(token):
        connected.set()
        return socket

    api.async_connect_websocket.side_effect = connect
    task = asyncio.create_task(client.async_run())
    await connected.wait()
    await asyncio.sleep(0)
    await client.async_stop()
    assert task.done()
    socket.close.assert_awaited_once()
    assert not client.connected


async def test_unload_bounds_unresponsive_socket_close(monkeypatch):
    monkeypatch.setattr(
        "custom_components.rhymax_mower.websocket.WS_CLOSE_TIMEOUT_SECONDS", 0.001
    )
    client, _, _, _ = client_and_api()
    socket = Socket()

    async def blocked_close():
        await asyncio.Event().wait()

    socket.close.side_effect = blocked_close
    client._socket = socket
    await asyncio.wait_for(client.async_stop(), 1)
    assert client._socket is None
    socket.close.assert_awaited_once()
