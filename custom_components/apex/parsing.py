# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import re
from typing import Any, NamedTuple

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfPower,
)

_RE_UNDERSCORE_NUM = re.compile(r"^(?P<base>[A-Za-z]+)_(?P<num>\d+)$")                 # Amp_14
_RE_TRAILING = re.compile(r"^(?P<base>[A-Za-z]+)(?:(?P<module>x\d+)|(?P<num>\d+))$")  # Tmp2, Alkx5, pHx8


class ProbeType(NamedTuple):
    label: str
    icon: str
    unit: str | None = None
    device_class: SensorDeviceClass | None = None


# by the probe type the Apex reports (lowercased)
PROBE_TYPES: dict[str, ProbeType] = {
    # the unit comes from temp_unit()
    "temp": ProbeType("Temp", "mdi:thermometer", None, SensorDeviceClass.TEMPERATURE),
    "ph": ProbeType("pH", "mdi:ph"),
    "amps": ProbeType("Current", "mdi:current-ac", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
    "cond": ProbeType("Conductivity", "mdi:flash"),
    # a cond probe named Salt...
    "salt": ProbeType("Salinity", "mdi:shaker-outline", "ppt"),
    "alk": ProbeType("Alkalinity", "mdi:test-tube", "dKH"),
    "ca": ProbeType("Calcium", "mdi:flask", "ppm"),
    "mg": ProbeType("Magnesium", "mdi:flask-outline", "ppm"),
    "orp": ProbeType("ORP", "mdi:test-tube", UnitOfElectricPotential.MILLIVOLT),
    "pwr": ProbeType("Power", "mdi:flash", UnitOfPower.WATT, SensorDeviceClass.POWER),
    "volts": ProbeType("Voltage", "mdi:sine-wave", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
    # liquid level sensor (LLS); the unit comes from module_unit()
    "in": ProbeType("Level", "mdi:waves-arrow-up", UnitOfLength.INCHES, SensorDeviceClass.DISTANCE),
    # flow sensor; no device class, since older Home Assistant doesn't know gal/h; the unit comes from module_unit()
    "gph": ProbeType("Flow", "mdi:waves-arrow-right", "gal/h"),
}


# the Apex reports "in" and "gph" whatever the FMM is set to; its levelMode / flowMode says the unit
MODULE_UNITS: dict[str, tuple[str, dict[str, str]]] = {
    "in": ("levelMode", {"in": UnitOfLength.INCHES, "cm": UnitOfLength.CENTIMETERS}),
    "gph": ("flowMode", {"gph": "gal/h", "lph": "L/h"}),
}

# what an FMM is assumed to be set to when its config can't be read (no password)
FMM_MODES: dict[str, dict[str, str]] = {
    "us": {"levelMode": "in", "flowMode": "gph"},
    "metric": {"levelMode": "cm", "flowMode": "lph"},
}


def module_unit(probe_type: str | None, module_extra: dict[str, Any] | None) -> str | None:
    """The unit an FMM probe reads in, from its module's config; None if unknown."""
    setting = MODULE_UNITS.get((probe_type or "").strip().lower())
    if not setting or not isinstance(module_extra, dict):
        return None
    key, units = setting
    return units.get(str(module_extra.get(key) or "").strip().lower())


def probe_type(probe_type: str | None, probe_name: str | None) -> ProbeType | None:
    t = (probe_type or "").strip().lower()
    if t == "cond" and (probe_name or "").strip().lower().startswith("salt"):
        t = "salt"
    return PROBE_TYPES.get(t)


def temp_unit(value: float | None, configured: str) -> str:
    # configured: "f", "c" or "auto"
    c = (configured or "").strip().upper()
    if c == "C":
        return "°C"
    if c == "F":
        return "°F"
    # heuristic: allow mixed °F/°C probes
    return "°C" if (value is not None and value <= 45) else "°F"


def split_probe_name(name: str) -> tuple[str, str | None, str | None]:
    m = _RE_UNDERSCORE_NUM.match(name)
    if m:
        return m.group("base"), m.group("num"), None

    m = _RE_TRAILING.match(name)
    if m:
        return m.group("base"), m.group("num"), m.group("module")

    return name, None, None


def pretty_type(probe_name: str, probe_type_name: str) -> str:
    info = probe_type(probe_type_name, probe_name)
    if info:
        return info.label
    return probe_type_name.strip() if probe_type_name else probe_name


def build_labels(probes: dict[str, dict[str, Any]]) -> dict[str, str]:
    base_for: dict[str, str] = {}
    suffix_for: dict[str, str] = {}
    is_amps: dict[str, bool] = {}

    for probe_name, p in probes.items():
        typ = (p.get("type") or "").strip()
        _, num, module = split_probe_name(probe_name)
        is_amps[probe_name] = typ.lower() == "amps"
        base_for[probe_name] = pretty_type(probe_name, typ)
        suffix_for[probe_name] = (num or module or "") if is_amps[probe_name] else (module or num or "")

    counts: dict[str, int] = {}
    # probes that would get the very same label
    unsuffixed: dict[str, int] = {}
    for probe_name, base in base_for.items():
        counts[base] = counts.get(base, 0) + 1
        if not suffix_for[probe_name]:
            unsuffixed[base] = unsuffixed.get(base, 0) + 1

    labels: dict[str, str] = {}
    for probe_name, base in base_for.items():
        suffix = suffix_for[probe_name]
        if is_amps[probe_name] and suffix:
            labels[probe_name] = f"{base} {suffix}"
        elif counts[base] > 1 and suffix:
            labels[probe_name] = f"{base} ({suffix})" if suffix.startswith("x") else f"{base} {suffix}"
        elif unsuffixed.get(base, 0) > 1:
            # e.g. per-outlet power on an EB832 ("Return_2_3W"): the Apex name is the best label
            labels[probe_name] = probes[probe_name].get("name") or probe_name
        else:
            labels[probe_name] = base

    return labels


def safe_uid_base(s: str) -> str:
    # keep stable unique_ids even if serial has ":" etc.
    return re.sub(r"[^a-z0-9_]+", "_", (s or "").lower()).strip("_")


def apex_uid(meta: dict[str, Any], host: str) -> str:
    """The Apex's id for unique_ids: its serial, or the host when it doesn't report one."""
    return safe_uid_base(meta.get("serial") or "") or safe_uid_base(host)
