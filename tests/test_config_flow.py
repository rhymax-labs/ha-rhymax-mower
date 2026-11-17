"""Config flow tests for zero-config PKCE OAuth2 linking."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rhymax_mower.api import RhymaxAccount
from custom_components.rhymax_mower.const import (
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_CLIENT_ID,
    OAUTH2_SCOPE,
    OAUTH2_TOKEN,
)
from custom_components.rhymax_mower.oauth import async_ensure_oauth_implementation


async def test_user_flow_goes_straight_to_pkce_authorize(hass: HomeAssistant) -> None:
    """Adding Rhymax Mower skips Application Credentials and emits PKCE S256."""
    hass.config.components.add("my")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["step_id"] == "auth"
    parsed = urlparse(result["url"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == OAUTH2_AUTHORIZE
    query = parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [OAUTH2_CLIENT_ID]
    assert query["redirect_uri"] == ["https://my.home-assistant.io/redirect/oauth"]
    assert query["scope"] == [OAUTH2_SCOPE]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) >= 43


async def test_oauth_implementation_registration_is_idempotent(
    hass: HomeAssistant,
) -> None:
    """Registering twice preserves a single built-in PKCE implementation."""
    await async_ensure_oauth_implementation(hass)
    first = await config_entry_oauth2_flow.async_get_implementations(hass, DOMAIN)
    await async_ensure_oauth_implementation(hass)
    second = await config_entry_oauth2_flow.async_get_implementations(hass, DOMAIN)

    assert list(first) == [DOMAIN]
    assert first[DOMAIN] is second[DOMAIN]
    assert isinstance(
        first[DOMAIN], config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce
    )


async def test_full_pkce_exchange_without_client_secret(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Code exchange sends code_verifier and omits client_secret."""
    hass.config.components.add("my")
    aioclient_mock.post(
        OAUTH2_TOKEN,
        json={
            "access_token": "pkce-access-token",
            "refresh_token": "pkce-refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    api = AsyncMock()
    api.async_get_account.return_value = RhymaxAccount("owner-pkce", [])

    with (
        patch(
            "custom_components.rhymax_mower.config_flow.RhymaxCloudApi",
            return_value=api,
        ),
        patch(
            "custom_components.rhymax_mower.async_setup_entry",
            return_value=True,
        ),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "code": "auth-code-123",
                "state": {
                    "flow_id": result["flow_id"],
                    "redirect_uri": "https://my.home-assistant.io/redirect/oauth",
                },
            },
        )
        assert result2["type"] is FlowResultType.EXTERNAL_STEP_DONE
        result3 = await hass.config_entries.flow.async_configure(result["flow_id"])
        await hass.async_block_till_done()

    assert result3["type"] is FlowResultType.CREATE_ENTRY
    assert result3["title"] == "RHYMAX account"
    assert result3["data"]["auth_implementation"] == DOMAIN
    assert len(aioclient_mock.mock_calls) == 1
    token_call_data = aioclient_mock.mock_calls[0][2]
    assert token_call_data["client_id"] == OAUTH2_CLIENT_ID
    assert "client_secret" not in token_call_data
    assert len(token_call_data["code_verifier"]) >= 43


def test_no_roboup_in_user_facing_strings_and_readme() -> None:
    """User-facing strings, translations, and README use RHYMAX rather than RoboUP."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    comp = root / "custom_components" / "rhymax_mower"
    for path in (
        comp / "strings.json",
        comp / "translations" / "en.json",
        comp / "translations" / "zh-Hans.json",
        root / "README.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert "RHYMAX" in text, f"{path} should reference RHYMAX"
        cleaned = text.replace("iroboup.com", "")
        assert "roboup" not in cleaned.lower(), f"{path} still mentions RoboUP"
