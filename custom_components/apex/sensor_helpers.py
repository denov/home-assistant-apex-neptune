# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfElectricCurrent


def icon_for_type(probe_type: str, probe_name: str) -> str | None:
    t = (probe_type or "").strip().lower()
    n = (probe_name or "").strip().lower()

    if t == "temp":
        return "mdi:thermometer"
    if t == "ph":
        return "mdi:ph"
    if t == "cond":
        return "mdi:shaker-outline" if n.startswith("salt") else "mdi:flash"
    if t == "amps":
        return "mdi:current-ac"
    if t == "alk":
        return "mdi:test-tube"
    if t == "ca":
        return "mdi:flask"
    if t == "mg":
        return "mdi:flask-outline"
    return "mdi:gauge"


def temp_unit(value: float | None, configured: str) -> str:
    # configured: "F" | "C" | "" (auto)
    c = (configured or "").strip().upper()
    if c == "C":
        return "°C"
    if c == "F":
        return "°F"
    # heuristic: allow mixed °F/°C probes
    return "°C" if (value is not None and value <= 45) else "°F"


def units_and_meta(
    probe_name: str,
    probe_type: str,
    value: float | None,
    overrides: dict[str, tuple[str, str | None]],
    configured_temp_unit: str,
) -> tuple[str | None, SensorDeviceClass | None, SensorStateClass | None]:
    if overrides and probe_name in overrides:
        unit = overrides[probe_name][1]
        return unit, None, SensorStateClass.MEASUREMENT

    t = (probe_type or "").strip().lower()
    n = probe_name.lower()

    if t == "amps":
        return UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT
    if t == "ph":
        return None, None, SensorStateClass.MEASUREMENT
    if t == "alk":
        return "dKH", None, SensorStateClass.MEASUREMENT
    if t in ("ca", "mg"):
        return "ppm", None, SensorStateClass.MEASUREMENT
    if t == "cond":
        return ("ppt" if n.startswith("salt") else None), None, SensorStateClass.MEASUREMENT
    if t == "temp":
        return temp_unit(value, configured_temp_unit), SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT

    return None, None, SensorStateClass.MEASUREMENT
