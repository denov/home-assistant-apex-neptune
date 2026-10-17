# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

DOMAIN = "apex"

# Optional overrides:
# { "ProbeName": ("Friendly Name", unit_or_None) }
# Example: { "Tmp": ("Temp", "°F") }
SENSOR_MAP: dict[str, tuple[str, str | None]] = {}

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
