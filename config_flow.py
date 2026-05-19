"""Config flow for the USEP integration — zero-configuration setup."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN


class USEPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    One-step setup: the integration needs no user input.
    Clicking Submit immediately creates the entry and all sensors.
    """

    VERSION = 1

    async def async_step_user(self, user_input=None):
        # Prevent more than one instance
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title="USEP — Singapore Electricity Price",
                data={},
            )

        # Show an empty confirmation form (no fields to fill in)
        return self.async_show_form(step_id="user")
