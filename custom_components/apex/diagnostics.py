# SPDX-License-Identifier: AGPL-3.0-or-later
"""Diagnostics download: what the integration read from the Apex, for bug reports."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_USERNAME
from .entity import ApexConfigEntry

# anywhere in the data, e.g. the Apex's own logins in /rest/config nconf
TO_REDACT = {
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_USERNAME,
    "host",
    "serial",
    # nconf: the Apex's network, web login, Wi-Fi and email settings
    "ipaddr",
    "gateway",
    "dns",
    "user",
    "wifiAPPassword",
    "ssid",
    "smtpServer",
    "emailFrom",
    "emailTo",
    "emailUser",
    "emailPassword",
    "linkKey",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ApexConfigEntry) -> dict[str, Any]:
    data = entry.runtime_data
    coordinator = data.coordinator
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "settings": asdict(data.settings),
            "rest_api": data.rest is not None,
            "last_update_success": coordinator.last_update_success,
            "failed_polls": coordinator.failed_polls,
            "data": coordinator.data,
        },
        TO_REDACT,
    )
