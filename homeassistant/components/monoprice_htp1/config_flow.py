"""Config flow for Monoprice HTP-1 integration."""

from __future__ import annotations

import aiodns
import aiohttp
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow as _ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .media_player import CannotConnect, Htp1

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_NAME): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """

    hostname = data[CONF_HOST]
    htp1 = Htp1(hostname=hostname)
    status = await htp1.connect()
    await htp1.stop()

    # Return info that you want to store in the config entry.
    name = data.get(CONF_NAME, data[CONF_HOST])

    return {
        "title": name,
    }


class ConfigFlow(_ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Monoprice HTP-1."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


    # allow reconfigure, using the same stuff as initial
    async_step_reconfigure = async_step_user
