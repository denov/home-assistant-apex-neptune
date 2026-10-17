# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_IP_ADDRESS,
    CONF_USERNAME,
    CONF_PASSWORD,
    DEFAULT_USERNAME,
    CONF_TEMP_UNIT,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    CONF_AMPS_DIAGNOSTIC,
    CONF_LINE_VOLTAGE,
    CONF_ENABLE_PER_OUTLET_POWER,
    CONF_READ_ONLY,
    ApexSettings,
)
from .coordinator import async_fetch_status
from .parsing import apex_uid
from .rest import ApexAuthError, ApexRestClient

CONNECTION_KEYS = (CONF_IP_ADDRESS, CONF_USERNAME, CONF_PASSWORD)

# never sent back to the browser: an empty field keeps the stored password
PASSWORD_SELECTOR = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))

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


def normalize_host(host: str) -> str:
    """apex.local or an IP address, also when pasted as a URL (http://apex.local/)."""
    host = (host or "").strip()
    for prefix in ("http://", "https://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    return host.split("/", 1)[0]


def _schema_user() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_IP_ADDRESS): str,
            vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
        }
    )


def _confirm_description(meta: dict[str, Any]) -> str:
    info = [
        f"{label}: {meta[key]}"
        for key, label in (("hostname", "Hostname"), ("serial", "Serial"), ("software", "Software"), ("hardware", "Hardware"))
        if meta.get(key)
    ]
    return "\n".join(info) if info else "Connected successfully."


def _schema_options(s: ApexSettings) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_IP_ADDRESS, default=s.host): str,
            vol.Optional(CONF_USERNAME, default=s.username): str,
            vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
            vol.Optional(CONF_TEMP_UNIT, default=s.temp_unit): TEMP_UNIT_SELECTOR,
            vol.Optional(CONF_SCAN_INTERVAL, default=str(s.scan_interval)): SCAN_INTERVAL_SELECTOR,
            vol.Optional(CONF_TIMEOUT, default=s.timeout): TIMEOUT_SELECTOR,
            vol.Optional(CONF_AMPS_DIAGNOSTIC, default=s.amps_diagnostic): selector.BooleanSelector(),
            vol.Optional(CONF_LINE_VOLTAGE, default=s.line_voltage): LINE_VOLTAGE_SELECTOR,
            vol.Optional(CONF_ENABLE_PER_OUTLET_POWER, default=s.per_outlet_energy): selector.BooleanSelector(),
            vol.Optional(CONF_READ_ONLY, default=s.read_only): selector.BooleanSelector(),
        }
    )


async def _async_validate(hass: HomeAssistant, s: ApexSettings) -> tuple[dict[str, Any], str | None]:
    """(the Apex's meta, error key or None) for these connection settings.

    With a password, it's checked against the REST login. Only a rejected login fails:
    other errors (e.g. firmware without the REST API) shouldn't block monitoring.
    """
    session = async_get_clientsession(hass)
    auth = aiohttp.BasicAuth(s.username, s.password) if s.password else None
    try:
        parsed = await async_fetch_status(session, s.status_url, auth=auth, timeout_seconds=s.timeout)
    except Exception:  # noqa: BLE001 - any failure means we can't use this Apex
        return {}, "cannot_connect"
    meta = parsed.get("meta") or {}

    if s.password:
        try:
            await ApexRestClient(session, s.host_url, s.username, s.password, s.timeout).async_login()
        except ApexAuthError:
            return meta, "invalid_auth"
        except Exception:  # noqa: BLE001
            pass
    return meta, None


class ApexConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._user_input: dict[str, Any] = {}
        self._meta: dict[str, Any] = {}

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = {
                CONF_PASSWORD: "",
                **user_input,
                CONF_IP_ADDRESS: normalize_host(user_input[CONF_IP_ADDRESS]),
            }
            ip = user_input[CONF_IP_ADDRESS]
            self._async_abort_entries_match({CONF_IP_ADDRESS: ip})

            self._meta, error = await _async_validate(self.hass, ApexSettings.from_dict(user_input))
            if error:
                errors["base"] = error
            else:
                # the serial, so the Apex is recognized at a new IP address (and that's saved)
                await self.async_set_unique_id(apex_uid(self._meta, ip))
                self._abort_if_unique_id_configured(updates={CONF_IP_ADDRESS: ip})
                self._user_input = user_input
                return await self.async_step_confirm()

        return self.async_show_form(step_id="user", data_schema=_schema_user(), errors=errors)

    async def async_step_confirm(self, user_input=None):
        # show a confirmation screen with detected meta
        if user_input is not None:
            # options left empty (user can configure via "Configure")
            return self.async_create_entry(title=f"Apex @ {self._user_input[CONF_IP_ADDRESS]}", data=self._user_input)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"info": _confirm_description(self._meta)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return ApexOptionsFlow()


class ApexOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        entry = self.config_entry
        # connection settings used to be saved in the options too
        current = {**entry.data, **entry.options}
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = {
                **user_input,
                CONF_IP_ADDRESS: normalize_host(user_input[CONF_IP_ADDRESS]),
                CONF_PASSWORD: user_input.get(CONF_PASSWORD) or current.get(CONF_PASSWORD, ""),
            }
            _, error = await _async_validate(self.hass, ApexSettings.from_dict(user_input))
            if error:
                errors["base"] = error
            else:
                # connection settings live in the entry's data, the rest in its options
                # (one update, so the entry reloads once)
                connection = {k: user_input[k] for k in CONNECTION_KEYS if k in user_input}
                options = {k: v for k, v in user_input.items() if k not in CONNECTION_KEYS}
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, **connection}, options=options)
                return self.async_create_entry(data=options)
            current = {**current, **user_input}

        return self.async_show_form(
            step_id="init", data_schema=_schema_options(ApexSettings.from_dict(current)), errors=errors
        )
