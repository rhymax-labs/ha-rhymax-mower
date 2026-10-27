"""Config flow — OAuth placeholder until gateway HA client is live."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            await self.async_set_unique_id("rhymax_mower_scaffold")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Rhymax Mower (scaffold)",
                data={},
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            description_placeholders={
                "note": "Cloud OAuth lands with gateway homeassistant client.",
            },
        )
