# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations


from datetime import timedelta

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_IP_ADDRESS,
    CONF_USERNAME,
    CONF_PASSWORD,
    DEFAULT_USERNAME,
    DEFAULT_PASSWORD,
    CONF_TEMP_UNIT,
    DEFAULT_TEMP_UNIT,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    CONF_TIMEOUT,
    DEFAULT_TIMEOUT,
    CONF_AMPS_DIAGNOSTIC,
    DEFAULT_AMPS_DIAGNOSTIC,
    CONF_LINE_VOLTAGE,
    DEFAULT_LINE_VOLTAGE,
    # optional:
    CONF_ENABLE_PER_OUTLET_POWER,
    DEFAULT_ENABLE_PER_OUTLET_POWER,
    CONF_READ_ONLY,
    DEFAULT_READ_ONLY,
)
from .coordinator import async_fetch_status
from .rest import ApexAuthError, ApexRestClient


TEMP_UNIT_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value="", label="Auto"),
            selector.SelectOptionDict(value="F", label="°F"),
            selector.SelectOptionDict(value="C", label="°C"),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)

SCAN_INTERVAL_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value="15", label="15 seconds"),
            selector.SelectOptionDict(value="30", label="30 seconds"),
            selector.SelectOptionDict(value="60", label="1 minute"),
            selector.SelectOptionDict(value="120", label="2 minutes"),
            selector.SelectOptionDict(value="300", label="5 minutes"),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)

TIMEOUT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=3, max=60, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s")
)

LINE_VOLTAGE_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=90, max=260, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="V")
)

AMPS_DIAGNOSTIC_SELECTOR = selector.BooleanSelector()
PER_OUTLET_POWER_SELECTOR = selector.BooleanSelector()
READ_ONLY_SELECTOR = selector.BooleanSelector()


def normalize_host(host: str) -> str:
    """apex.local or an IP address, also when pasted as a URL (http://apex.local/)."""
    host = (host or "").strip()
    for prefix in ("http://", "https://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    return host.split("/", 1)[0]


def _schema_user():
    return vol.Schema(
        {
            vol.Required(CONF_IP_ADDRESS): str,
            vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
        }
    )


def _schema_confirm(meta: dict):
    # Read-only info
    info = []
    if meta.get("hostname"):
        info.append(f"Hostname: {meta['hostname']}")
    if meta.get("serial"):
        info.append(f"Serial: {meta['serial']}")
    if meta.get("software"):
        info.append(f"Software: {meta['software']}")
    if meta.get("hardware"):
        info.append(f"Hardware: {meta['hardware']}")
    description = "\n".join(info) if info else "Connected successfully."

    return vol.Schema({}) , description


def _schema_options(merged: dict):
    return vol.Schema(
        {
            vol.Required(CONF_IP_ADDRESS, default=merged.get(CONF_IP_ADDRESS, "")): str,
            vol.Optional(CONF_USERNAME, default=merged.get(CONF_USERNAME, DEFAULT_USERNAME)): str,
            vol.Optional(CONF_PASSWORD, default=merged.get(CONF_PASSWORD, DEFAULT_PASSWORD)): str,

            vol.Optional(CONF_TEMP_UNIT, default=merged.get(CONF_TEMP_UNIT, DEFAULT_TEMP_UNIT)): TEMP_UNIT_SELECTOR,

            vol.Optional(CONF_SCAN_INTERVAL, default=str(merged.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))): SCAN_INTERVAL_SELECTOR,
            vol.Optional(CONF_TIMEOUT, default=int(merged.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))): TIMEOUT_SELECTOR,

            vol.Optional(CONF_AMPS_DIAGNOSTIC, default=bool(merged.get(CONF_AMPS_DIAGNOSTIC, DEFAULT_AMPS_DIAGNOSTIC))): AMPS_DIAGNOSTIC_SELECTOR,

            vol.Optional(CONF_LINE_VOLTAGE, default=int(merged.get(CONF_LINE_VOLTAGE, DEFAULT_LINE_VOLTAGE))): LINE_VOLTAGE_SELECTOR,

            # optional:
            vol.Optional(CONF_ENABLE_PER_OUTLET_POWER, default=bool(merged.get(CONF_ENABLE_PER_OUTLET_POWER, DEFAULT_ENABLE_PER_OUTLET_POWER))): PER_OUTLET_POWER_SELECTOR,

            vol.Optional(CONF_READ_ONLY, default=bool(merged.get(CONF_READ_ONLY, DEFAULT_READ_ONLY))): READ_ONLY_SELECTOR,
        }
    )


class ApexConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._user_input: dict | None = None
        self._meta: dict = {}
        self._error: str | None = None

    async def async_step_user(self, user_input=None):
        self._error = None

        if user_input is not None:
            user_input = {**user_input, CONF_IP_ADDRESS: normalize_host(user_input[CONF_IP_ADDRESS])}
            ip = user_input[CONF_IP_ADDRESS]
            await self.async_set_unique_id(ip)
            self._abort_if_unique_id_configured()

            # Test connection now
            ok = await self._async_test_connection(user_input)
            if not ok:
                return self.async_show_form(step_id="user", data_schema=_schema_user(), errors={"base": "cannot_connect"})
            if not await self._async_test_login(user_input):
                return self.async_show_form(step_id="user", data_schema=_schema_user(), errors={"base": "invalid_auth"})
            self._user_input = user_input
            return await self.async_step_confirm()

        return self.async_show_form(step_id="user", data_schema=_schema_user(), errors={})

    async def async_step_confirm(self, user_input=None):
        # show a confirmation screen with detected meta
        if user_input is not None:
            # Create entry; options left empty (user can configure via "Configure")
            ip = self._user_input[CONF_IP_ADDRESS] if self._user_input else "Apex"
            title = f"Apex @ {ip}"
            return self.async_create_entry(title=title, data=self._user_input or {})

        schema, desc = _schema_confirm(self._meta)
        return self.async_show_form(
            step_id="confirm",
            data_schema=schema,
            description_placeholders={"info": desc},
        )

    async def _async_test_connection(self, user_input: dict) -> bool:
        ip = user_input[CONF_IP_ADDRESS]
        username = user_input.get(CONF_USERNAME, DEFAULT_USERNAME)
        password = user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD)
        session = async_get_clientsession(self.hass)

        auth = None
        if password:
            # only if user set one
            import aiohttp
            auth = aiohttp.BasicAuth(username or DEFAULT_USERNAME, password)

        try:
            parsed = await async_fetch_status(session, f"http://{ip}/cgi-bin", auth=auth, timeout_seconds=10)
            self._meta = parsed.get("meta", {}) or {}
            return True
        except Exception:
            self._meta = {}
            return False

    async def _async_test_login(self, user_input: dict) -> bool:
        """With a password, check it against the REST login used for feed control.

        Only a rejected login fails: other errors (e.g. firmware without the REST API)
        shouldn't block monitoring.
        """
        password = user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD)
        if not password:
            return True
        client = ApexRestClient(
            async_get_clientsession(self.hass),
            f"http://{user_input[CONF_IP_ADDRESS]}",
            user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
            password,
        )
        try:
            await client.async_login()
        except ApexAuthError:
            return False
        except Exception:
            pass
        return True

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return ApexOptionsFlow(config_entry)


class ApexOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry):
        self._entry = entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            user_input = {**user_input, CONF_IP_ADDRESS: normalize_host(user_input[CONF_IP_ADDRESS])}
            return self.async_create_entry(title="", data=user_input)

        merged = {**self._entry.data, **self._entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema_options(merged), errors={})
