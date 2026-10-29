"""Shared REST/push projection and real Home Assistant OAuth regression tests."""

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientResponseError, ClientSession
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from custom_components.rhymax_mower.api import (
    ConfigEntryAuth,
    RhymaxApiError,
    RhymaxAuthError,
    RhymaxCloudApi,
    RhymaxConnectionError,
    RhymaxSubscriptionError,
    StaticTokenAuth,
    parse_state,
)
from custom_components.rhymax_mower.const import API_BASE_URL, WEBSOCKET_URL

MAC = "aabbccddeeff"


@pytest.fixture
def state_payload():
    return {
        "id": MAC,
        "online": True,
        "last_activity_ts": 1_800_000_000_000,
        "activity": "mowing",
        "task_state": 1,
        "sub_state": 2,
        "battery_level": 80,
        "is_docked": False,
        "is_charging": False,
        "progress_percent": 40,
    }


def test_state_projection_and_offline_clear(state_payload):
    online = parse_state({**state_payload, "robot_status": {"private": 1}})
    assert asdict(online) == state_payload
    offline = asdict(parse_state({**state_payload, "online": False}))
    assert offline["id"] == MAC
    assert offline["last_activity_ts"] == state_payload["last_activity_ts"]
    assert offline["online"] is False
    assert all(
        value is None
        for key, value in offline.items()
        if key not in {"id", "online", "last_activity_ts"}
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"online": None},
        {"online": "false"},
        {"online": 1},
        {"id": "AABBCCDDEEFF"},
        {"id": ""},
        {"last_activity_ts": True},
        {"battery_level": 101},
        {"progress_percent": -1},
        {"task_state": 4},
        {"sub_state": "1"},
        {"is_docked": 1},
        {"activity": {}},
    ],
)
def test_malformed_state_never_becomes_offline(state_payload, changes):
    with pytest.raises(RhymaxApiError):
        parse_state({**state_payload, **changes})


def test_missing_presence_mismatched_mac_and_unknown_activity(state_payload):
    del state_payload["online"]
    with pytest.raises(RhymaxApiError):
        parse_state(state_payload)
    state_payload["online"] = True
    with pytest.raises(RhymaxApiError):
        parse_state(state_payload, expected_id="112233445566")
    state_payload["activity"] = "new_server_activity"
    assert parse_state(state_payload).activity == "other"


def transport_response(body, status=200, headers=None):
    response = MagicMock(status=status, headers=headers or {})
    response.json = AsyncMock(return_value=body)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


async def test_rest_state_uses_ha_path_and_shared_parser(state_payload):
    session = MagicMock(spec=ClientSession)
    session.request.return_value = transport_response(state_payload)
    api = RhymaxCloudApi(session, StaticTokenAuth("ha-oauth"))
    assert await api.async_get_state(MAC) == parse_state(state_payload)
    args, kwargs = session.request.call_args
    assert args == ("GET", f"{API_BASE_URL}/api/v1/homeassistant/devices/{MAC}/state")
    assert kwargs["headers"] == {"Authorization": "Bearer ha-oauth"}


async def test_429_retains_retry_after():
    session = MagicMock(spec=ClientSession)
    session.request.return_value = transport_response(
        {"code": "rate_limited"}, 429, {"Retry-After": "45"}
    )
    api = RhymaxCloudApi(session, StaticTokenAuth("ha-oauth"))
    with pytest.raises(RhymaxApiError) as caught:
        await api.async_get_state(MAC)
    assert caught.value.status == 429
    assert caught.value.retry_after == 45


async def test_rest_static_rejected_token_never_retried():
    session = MagicMock(spec=ClientSession)
    session.request.return_value = transport_response({}, 401)
    api = RhymaxCloudApi(session, StaticTokenAuth("rejected"))
    with pytest.raises(RhymaxAuthError):
        await api.async_get_state(MAC)
    assert session.request.call_count == 1


async def test_ha_oauth_refresh_shared_between_concurrent_rest_and_ws(hass, entry):
    implementation = MagicMock()
    implementation.async_refresh_token = AsyncMock(
        return_value={
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_at": 4_000_000_001,
            "expires_in": 3600,
        }
    )
    oauth = OAuth2Session(hass, entry, implementation)
    api = RhymaxCloudApi(MagicMock(spec=ClientSession), ConfigEntryAuth(oauth))
    tokens = await asyncio.gather(
        *(api.async_refresh_access_token("test-ha-access") for _ in range(5))
    )
    assert tokens == ["rotated-access"] * 5
    implementation.async_refresh_token.assert_awaited_once()
    assert entry.data["token"]["refresh_token"] == "rotated-refresh"
    assert await api.async_get_access_token() == "rotated-access"


async def test_proactive_ha_oauth_refresh(hass, entry):
    import time

    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "token": {**entry.data["token"], "expires_at": time.time() + 45},
        },
    )
    implementation = MagicMock()
    implementation.async_refresh_token = AsyncMock(
        return_value={
            **entry.data["token"],
            "access_token": "renewed",
            "expires_at": 4_000_000_000,
        }
    )
    api = RhymaxCloudApi(
        MagicMock(spec=ClientSession),
        ConfigEntryAuth(OAuth2Session(hass, entry, implementation)),
    )
    assert await api.async_get_access_token() == "renewed"
    implementation.async_refresh_token.assert_awaited_once()


async def test_ws_auth_header_namespace_and_no_subprotocol():
    session = MagicMock(spec=ClientSession)
    session.ws_connect = AsyncMock()
    api = RhymaxCloudApi(session, StaticTokenAuth("ha-oauth"))
    await api.async_connect_websocket(await api.async_get_access_token())
    args, kwargs = session.ws_connect.call_args
    assert args == (WEBSOCKET_URL,)
    assert kwargs["headers"] == {"Authorization": "Bearer ha-oauth"}
    assert "protocols" not in kwargs
    assert "params" not in kwargs
    assert kwargs["heartbeat"] > 0


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, RhymaxAuthError),
        (403, RhymaxSubscriptionError),
        (503, RhymaxConnectionError),
    ],
)
async def test_ws_handshake_error_classification(status, error):
    session = MagicMock(spec=ClientSession)
    session.ws_connect = AsyncMock(
        side_effect=ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=status,
        )
    )
    api = RhymaxCloudApi(session, StaticTokenAuth("ha-oauth"))
    with pytest.raises(error):
        await api.async_connect_websocket("ha-oauth")


async def test_ws_handshake_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(
        "custom_components.rhymax_mower.api.WS_CONNECT_TIMEOUT_SECONDS", 0.001
    )

    async def blocked_connect(*args, **kwargs):
        await asyncio.Event().wait()

    session = MagicMock(spec=ClientSession)
    session.ws_connect = AsyncMock(side_effect=blocked_connect)
    api = RhymaxCloudApi(session, StaticTokenAuth("ha-oauth"))
    with pytest.raises(RhymaxConnectionError):
        await api.async_connect_websocket("ha-oauth")


async def test_rest_refresh_retry_uses_shared_new_token(hass, entry, state_payload):
    session = MagicMock(spec=ClientSession)
    session.request.side_effect = [
        transport_response({}, 401),
        transport_response(state_payload),
    ]
    implementation = MagicMock()
    implementation.async_refresh_token = AsyncMock(
        return_value={
            **entry.data["token"],
            "access_token": "rotated",
            "refresh_token": "rotated-refresh",
        }
    )
    api = RhymaxCloudApi(
        session, ConfigEntryAuth(OAuth2Session(hass, entry, implementation))
    )
    assert await api.async_get_state(MAC) == parse_state(state_payload)
    assert [
        call.kwargs["headers"]["Authorization"]
        for call in session.request.call_args_list
    ] == [
        "Bearer test-ha-access",
        "Bearer rotated",
    ]
    assert await api.async_get_access_token() == "rotated"
    implementation.async_refresh_token.assert_awaited_once()


async def test_rest_redirect_is_error_and_credentials_stay_on_ha_endpoint():
    session = MagicMock(spec=ClientSession)
    session.request.return_value = transport_response({}, 302)
    api = RhymaxCloudApi(session, StaticTokenAuth("ha-oauth"))
    with pytest.raises(RhymaxApiError) as caught:
        await api.async_get_state(MAC)
    assert caught.value.status == 302
    assert session.request.call_args.kwargs["allow_redirects"] is False
