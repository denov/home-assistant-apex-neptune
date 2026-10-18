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
CONF_REMOVE_PASSWORD = "remove_password"

# never sent back to the browser: an empty field keeps the stored password
PASSWORD_SELECTOR = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))

# option labels are in translations/en.json under "selector"
TEMP_UNIT_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=["auto", "f", "c"], mode=selector.SelectSelectorMode.DROPDOWN, translation_key="temp_unit"
    )
)

SCAN_INTERVAL_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=["15", "30", "60", "120", "300"],
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="scan_interval",
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


def _confirm_placeholders(meta: dict[str, Any]) -> dict[str, str]:
    """What the confirm step shows (its text is in translations/en.json); "-" for what the Apex didn't report."""
    return {key: str(meta.get(key) or "-") for key in ("hostname", "serial", "software", "hardware")}


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


def _with_connection(
    entry: config_entries.ConfigEntry, connection: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The entry's (data, options) with these connection settings.

    They live in the data; older versions also saved them in the options, where they'd win.
    """
    return (
        {**entry.data, **connection},
        {k: v for k, v in entry.options.items() if k not in CONNECTION_KEYS},
    )


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
            description_placeholders=_confirm_placeholders(self._meta),
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """The Apex rejected the password (started by the coordinator or a control action)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        current = {**entry.data, **entry.options}
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input.get(CONF_PASSWORD) or ""
            if user_input.get(CONF_REMOVE_PASSWORD):
                # monitor only: status pages don't need the password
                password = ""
            elif not password:
                errors["base"] = "password_required"
            else:
                _, error = await _async_validate(self.hass, ApexSettings.from_dict({**current, CONF_PASSWORD: password}))
                if error:
                    errors["base"] = error

            if not errors:
                data, options = _with_connection(entry, {CONF_PASSWORD: password})
                if options != dict(entry.options):
                    self.hass.config_entries.async_update_entry(entry, options=options)
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
                    vol.Optional(CONF_REMOVE_PASSWORD, default=False): selector.BooleanSelector(),
                }
            ),
            description_placeholders={
                "host": current[CONF_IP_ADDRESS],
                "username": current.get(CONF_USERNAME) or DEFAULT_USERNAME,
            },
            errors=errors,
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
                data, _ = _with_connection(entry, {k: user_input[k] for k in CONNECTION_KEYS if k in user_input})
                options = {k: v for k, v in user_input.items() if k not in CONNECTION_KEYS}
                self.hass.config_entries.async_update_entry(entry, data=data, options=options)
                return self.async_create_entry(data=options)
            current = {**current, **user_input}

        return self.async_show_form(
            step_id="init", data_schema=_schema_options(ApexSettings.from_dict(current)), errors=errors
        )
