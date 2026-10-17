# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DOMAIN = "apex"

# Config entry keys
CONF_IP_ADDRESS = "ip_address"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# Options / advanced settings
CONF_TEMP_UNIT = "temp_unit"                # "", "F", "C"
CONF_SCAN_INTERVAL = "scan_interval"        # seconds (stored as string in options UI)
CONF_TIMEOUT = "timeout"                    # seconds
CONF_AMPS_DIAGNOSTIC = "amps_diagnostic"    # bool (mark Amp_* as diagnostic)
CONF_LINE_VOLTAGE = "line_voltage"          # volts (for total power W = A * V)
CONF_ENABLE_PER_OUTLET_POWER = "enable_per_outlet_power"
CONF_READ_ONLY = "read_only"                # bool: monitor only, never change the Apex


# Defaults
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = ""

DEFAULT_TEMP_UNIT = ""          # auto
DEFAULT_SCAN_INTERVAL = 30      # seconds
DEFAULT_TIMEOUT = 10            # seconds
DEFAULT_AMPS_DIAGNOSTIC = True  # keep Current/Amp_* under Diagnostic by default
DEFAULT_LINE_VOLTAGE = 120      # US default; set 230 for EU
DEFAULT_ENABLE_PER_OUTLET_POWER = False
DEFAULT_READ_ONLY = False


@dataclass(frozen=True)
class ApexSettings:
    """A config entry's data and options, with defaults filled in."""

    host: str
    username: str = DEFAULT_USERNAME
    password: str = DEFAULT_PASSWORD
    temp_unit: str = DEFAULT_TEMP_UNIT
    scan_interval: int = DEFAULT_SCAN_INTERVAL
    timeout: int = DEFAULT_TIMEOUT
    amps_diagnostic: bool = DEFAULT_AMPS_DIAGNOSTIC
    line_voltage: int = DEFAULT_LINE_VOLTAGE
    per_outlet_energy: bool = DEFAULT_ENABLE_PER_OUTLET_POWER
    read_only: bool = DEFAULT_READ_ONLY

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ApexSettings:
        return cls(
            host=d[CONF_IP_ADDRESS],
            username=d.get(CONF_USERNAME) or DEFAULT_USERNAME,
            password=d.get(CONF_PASSWORD) or DEFAULT_PASSWORD,
            temp_unit=d.get(CONF_TEMP_UNIT, DEFAULT_TEMP_UNIT),
            scan_interval=int(d.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
            timeout=int(d.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
            amps_diagnostic=bool(d.get(CONF_AMPS_DIAGNOSTIC, DEFAULT_AMPS_DIAGNOSTIC)),
            line_voltage=int(d.get(CONF_LINE_VOLTAGE, DEFAULT_LINE_VOLTAGE)),
            per_outlet_energy=bool(d.get(CONF_ENABLE_PER_OUTLET_POWER, DEFAULT_ENABLE_PER_OUTLET_POWER)),
            read_only=bool(d.get(CONF_READ_ONLY, DEFAULT_READ_ONLY)),
        )

    @property
    def host_url(self) -> str:
        return f"http://{self.host}"

    @property
    def status_url(self) -> str:
        """Where status.json and status.xml are."""
        return f"{self.host_url}/cgi-bin"
