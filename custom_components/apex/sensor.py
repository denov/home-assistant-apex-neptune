# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import callback
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent, UnitOfEnergy, UnitOfPower, UnitOfVolume
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.dt import now as dt_now

from .entity import (
    DOS_TYPES,
    ApexConfigEntry,
    ApexEntity,
    ApexOutputEntity,
    ApexRuntimeData,
    async_add_discovered,
    find_output_config,
    output_key,
)
from .parsing import FMM_MODES, build_labels, module_unit, probe_type, temp_unit
from .rest import FEED_LETTERS

# outputs whose program is usually just "Set <value>"
VARIABLE_TYPES = ("variable", "virtual")

TRIDENT_TYPES = ("TRI", "TNP", "TH")
# status extra.levels, as the Apex dashboard reads it: [waste used, ?, reagent C, reagent B, reagent A]
TRIDENT_WASTE = 0
TRIDENT_REAGENTS = (4, 3, 2)  # A (1), B (2), C (3)
# the dashboard counts tests remaining from at most this many mL per reagent
TRIDENT_MAX_REAGENT_ML = 230


def trident_reagent_names(hwtype: str) -> tuple[str, str, str]:
    return ("1", "2", "3") if hwtype == "TNP" else ("A", "B", "C")


def trident_ml_per_test(hwtype: str) -> float:
    return 3.652 if hwtype == "TNP" else 1.9


def _number(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _probes(data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return (data or {}).get("probes", {}) or {}


def total_current_amps(probes: dict[str, dict[str, Any]]) -> float | None:
    """Sum of all probes of type Amps, or None when there are none."""
    amps = [
        v
        for p in probes.values()
        if (p.get("type") or "").strip().lower() == "amps" and (v := _number(p.get("value"))) is not None
    ]
    return sum(amps) if amps else None


def output_intensity(o: dict[str, Any]) -> float | None:
    """Intensity (%) of a pump, light or 0-10V output, when the Apex reports one."""
    if o.get("intensity") is not None:
        return o["intensity"]
    if (o.get("type") or "").lower() in (*DOS_TYPES, *VARIABLE_TYPES, "outlet", "alert"):
        return None
    status = o.get("status") or []
    try:
        return float(status[1]) if len(status) > 1 and status[1] else None
    except ValueError:
        return None


def dos_volumes(o: dict[str, Any]) -> tuple[float | None, float | None]:
    """(capacity, volume left) in mL from a DOS/DQD output's status."""
    status = o.get("status") or []

    def num(i: int) -> float | None:
        try:
            return float(status[i]) if len(status) > i and status[i] else None
        except ValueError:
            return None

    return num(3), num(4)


_SET_RE = re.compile(r"^\s*Set\s+(\S+)\s*$", re.IGNORECASE)


def program_setting(prog: str | None) -> int | float | str | None:
    """What a "Set ..." program sets: a number (Set 75), or a profile or state (Set PF1, Set ON)."""
    lines = [line for line in (prog or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    m = _SET_RE.match(lines[0])
    if not m:
        return None
    value = m.group(1)
    try:
        number = float(value)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _module_config(data: dict[str, Any] | None, abaddr: Any) -> dict[str, Any]:
    config = (data or {}).get("config") or {}
    for m in config.get("mconf") or []:
        if isinstance(m, dict) and m.get("abaddr") == abaddr:
            return m
    return {}


def _input_module_config(data: dict[str, Any] | None, input_name: str) -> dict[str, Any]:
    """The config of the module an input is on, by its device id (e.g. "1_P0" is on module 1)."""
    config = (data or {}).get("config") or {}
    for i in config.get("iconf") or []:
        if isinstance(i, dict) and i.get("name") == input_name:
            addr = str(i.get("did") or "").partition("_")[0]
            return _module_config(data, int(addr)) if addr.isdigit() else {}
    return {}


def _module_name(data: dict[str, Any], module: dict[str, Any]) -> str:
    name = _module_config(data, module.get("abaddr")).get("name")
    return str(name) if name else f"{module.get('hwtype')}_{module.get('abaddr')}"


def _find_module(data: dict[str, Any] | None, abaddr: Any) -> dict[str, Any]:
    for m in (data or {}).get("modules", []) or []:
        if m.get("abaddr") == abaddr:
            return m
    return {}


def _epoch_to_datetime(epoch: Any) -> datetime | None:
    if _number(epoch) is None or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc)


async def async_setup_entry(hass, entry: ApexConfigEntry, async_add_entities):
    data = entry.runtime_data
    settings = data.settings

    def discover(d: dict[str, Any]):
        yield "total_current", lambda: ApexTotalCurrentSensor(data)
        yield "total_power", lambda: ApexTotalPowerSensor(data)
        yield "total_energy", lambda: ApexTotalEnergySensor(data)

        # Probes (auto-discovered)
        probes = _probes(d)
        labels = build_labels(probes)
        for name in sorted(probes):
            label = labels.get(name) or name
            yield name, lambda name=name, label=label: ApexProbeSensor(data, name, probes[name], label)
            # Energy for each outlet the Apex measures the power of (EB832)
            if settings.per_outlet_energy and (probes[name].get("type") or "").strip().lower() == "pwr":
                yield f"{name}_energy", lambda name=name, label=label: ApexProbeEnergySensor(data, name, f"{label} Energy")

        # Power timestamps (appear only if present; will be added later if they show up)
        power = d.get("power") or {}
        for kind in ("failed", "restored"):
            if power.get(kind):
                yield f"power_{kind}", lambda kind=kind: ApexPowerTimeSensor(data, kind)

        # Feed cycle (status.json only)
        if d.get("feed") is not None:
            yield "feed_mode", lambda: ApexFeedSensor(data)
            yield "feed_ends", lambda: ApexFeedEndsSensor(data)

        for o in d.get("outlets", []) or []:
            key = output_key(o)
            if not key:
                continue
            typ = (o.get("type") or "").lower()
            name = o.get("name") or key
            if typ in DOS_TYPES and dos_volumes(o)[1] is not None:
                yield f"output_{key}_volume_left", lambda key=key, name=name: ApexDosVolumeSensor(data, key, name)
            elif output_intensity(o) is not None:
                yield f"output_{key}_intensity", lambda key=key, name=name: ApexIntensitySensor(data, key, name)
            if typ in VARIABLE_TYPES and find_output_config(d, o.get("device_id")):
                yield f"output_{key}_setting", lambda key=key, name=name: ApexSettingSensor(data, key, name)

        # Trident levels (REST status, so only with a password)
        for m in d.get("modules", []) or []:
            hwtype = m.get("hwtype")
            levels = (m.get("extra") or {}).get("levels")
            if hwtype not in TRIDENT_TYPES or not isinstance(levels, list) or len(levels) < 5:
                continue
            abaddr, module_name = m["abaddr"], _module_name(d, m)
            for letter, index in zip(trident_reagent_names(hwtype), TRIDENT_REAGENTS):
                yield (
                    f"trident_{abaddr}_reagent_{letter}",
                    lambda letter=letter, index=index, abaddr=abaddr, module_name=module_name: (
                        ApexTridentReagentSensor(data, abaddr, module_name, letter, index)
                    ),
                )
            yield (
                f"trident_{abaddr}_waste",
                lambda abaddr=abaddr, module_name=module_name: ApexTridentWasteSensor(data, abaddr, module_name),
            )

    async_add_discovered(entry, async_add_entities, discover)


class ApexProbeSensor(ApexEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, data: ApexRuntimeData, probe_name: str, probe: dict[str, Any], label: str):
        super().__init__(data, probe_name, label)
        self._probe = probe_name
        self._probe_type = (probe.get("type") or "").strip()
        self._fmm_modes = FMM_MODES.get(data.settings.fmm_units)

        info = probe_type(self._probe_type, probe_name)
        self._attr_icon = info.icon if info else "mdi:gauge"
        if info:
            self._attr_device_class = info.device_class
            self._attr_native_unit_of_measurement = info.unit
        if self._probe_type.lower() == "temp":
            self._attr_native_unit_of_measurement = temp_unit(probe.get("value"), data.settings.temp_unit)
        if self._probe_type.lower() == "amps" and data.settings.amps_diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _p(self) -> dict[str, Any]:
        return _probes(self.coordinator.data).get(self._probe) or {}

    @property
    def native_value(self):
        return self._p().get("value")

    @property
    def native_unit_of_measurement(self):
        # e.g. an LLS on an FMM set to cm; until the REST config is read (never without a password),
        # what the fmm_units option says
        extra = _input_module_config(self.coordinator.data, self._p().get("name") or self._probe).get("extra")
        return (
            module_unit(self._probe_type, extra)
            or module_unit(self._probe_type, self._fmm_modes)
            or super().native_unit_of_measurement
        )

    @property
    def extra_state_attributes(self):
        return {"apex_probe_name": self._p().get("name") or self._probe, "apex_probe_type": self._probe_type}


class ApexPowerTimeSensor(ApexEntity, SensorEntity):
    """Diagnostic text sensor for when the power last failed or was restored."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, data: ApexRuntimeData, kind: str):
        # kind: "failed" or "restored"
        super().__init__(data, f"power_{kind}", f"Power {kind.capitalize()}")
        self._kind = kind
        self._attr_icon = "mdi:power-plug-off" if kind == "failed" else "mdi:power-plug"

    @property
    def native_value(self):
        return ((self.coordinator.data or {}).get("power") or {}).get(self._kind)


class ApexTotalCurrentSensor(ApexEntity, SensorEntity):
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:current-ac"

    def __init__(self, data: ApexRuntimeData):
        super().__init__(data, "total_current", "Total Current")

    @property
    def native_value(self):
        amps = total_current_amps(_probes(self.coordinator.data))
        return round(amps, 3) if amps is not None else None


class ApexTotalPowerSensor(ApexEntity, SensorEntity):
    """Total current x the configured line voltage."""

    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    def __init__(self, data: ApexRuntimeData):
        super().__init__(data, "total_power", "Total Power")
        self._v = float(data.settings.line_voltage)

    @property
    def native_value(self):
        amps = total_current_amps(_probes(self.coordinator.data))
        return round(amps * self._v, 1) if amps is not None else None

    @property
    def extra_state_attributes(self):
        return {"line_voltage": self._v}


class ApexEnergySensor(ApexEntity, RestoreEntity, SensorEntity):
    """Accumulates kWh from power readings (W) over time."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"

    # a longer gap between readings (e.g. Home Assistant was stopped) isn't counted
    _MAX_GAP_SECONDS = 3600

    def __init__(self, data: ApexRuntimeData, key: str, name: str):
        super().__init__(data, key, name)
        self._kwh = 0.0
        self._last_ts: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        try:
            self._kwh = float(state.state) if state else 0.0
        except ValueError:
            # unknown or unavailable
            self._kwh = 0.0
        self._last_ts = dt_now()

    def _current_watts(self) -> float | None:
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self.coordinator.last_update_success:
            # the readings are stale: count nothing until the Apex answers again
            self._last_ts = None
            self.async_write_ha_state()
            return

        now_ts = dt_now()
        last_ts, self._last_ts = self._last_ts, now_ts
        watts = self._current_watts()
        if last_ts is not None and watts is not None:
            dt_seconds = (now_ts - last_ts).total_seconds()
            if 0 < dt_seconds <= self._MAX_GAP_SECONDS:
                self._kwh += watts * dt_seconds / 3600 / 1000
        self.async_write_ha_state()

    @property
    def native_value(self):
        return round(self._kwh, 6)


class ApexTotalEnergySensor(ApexEnergySensor):
    """Energy from Total Power (all Amps probes x line voltage)."""

    def __init__(self, data: ApexRuntimeData):
        super().__init__(data, "total_energy", "Total Energy")
        self._v = float(data.settings.line_voltage)

    def _current_watts(self) -> float | None:
        amps = total_current_amps(_probes(self.coordinator.data))
        return amps * self._v if amps is not None else None


class ApexProbeEnergySensor(ApexEnergySensor):
    """Energy from one outlet's power probe (EB832 "pwr" inputs)."""

    def __init__(self, data: ApexRuntimeData, probe_name: str, label: str):
        super().__init__(data, f"{probe_name}_energy", label)
        self._probe = probe_name

    def _current_watts(self) -> float | None:
        return _number((_probes(self.coordinator.data).get(self._probe) or {}).get("value"))


class ApexOutputSensor(ApexOutputEntity, SensorEntity):
    """A reading from an output's status (keyed like the outlet switch: output ID, else name)."""

    def __init__(self, data: ApexRuntimeData, key: str, kind: str, name: str):
        super().__init__(data, key, f"output_{key}_{kind}", name)


class ApexDosVolumeSensor(ApexOutputSensor):
    """Volume left in a DOS/DQD reservoir, as the Apex counts it down from the configured size."""

    _attr_native_unit_of_measurement = UnitOfVolume.MILLILITERS
    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cup-water"

    def __init__(self, data: ApexRuntimeData, key: str, output_name: str):
        super().__init__(data, key, "volume_left", f"{output_name} Volume Left")

    @property
    def native_value(self):
        return dos_volumes(self._output())[1]

    @property
    def extra_state_attributes(self):
        capacity, left = dos_volumes(self._output())
        percent = round(left / capacity * 100, 1) if capacity and left is not None else None
        return {"capacity_ml": capacity, "percent_left": percent, "device_id": self._output().get("device_id")}


class ApexIntensitySensor(ApexOutputSensor):
    """Intensity of a pump, light or variable-speed output."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:pump"

    def __init__(self, data: ApexRuntimeData, key: str, output_name: str):
        super().__init__(data, key, "intensity", f"{output_name} Intensity")

    @property
    def native_value(self):
        return output_intensity(self._output())

    @property
    def extra_state_attributes(self):
        o = self._output()
        return {"apex_type": o.get("type"), "raw_status": o.get("status"), "device_id": o.get("device_id")}


class ApexSettingSensor(ApexOutputSensor):
    """What a variable's program sets ("Set 75" is 75, "Set PF1" is PF1); the program is an attribute.

    Read from the Apex configuration, which is refreshed every few minutes.
    """

    _attr_icon = "mdi:variable"

    def __init__(self, data: ApexRuntimeData, key: str, output_name: str):
        super().__init__(data, key, "setting", f"{output_name} Setting")

    def _config(self) -> dict[str, Any]:
        return find_output_config(self.coordinator.data, self._output().get("device_id"))

    @property
    def native_value(self):
        return program_setting(self._config().get("prog"))

    @property
    def extra_state_attributes(self):
        c = self._config()
        return {"program": c.get("prog"), "control_type": c.get("ctype"), "device_id": c.get("did")}


class ApexTridentSensor(ApexEntity, SensorEntity):
    _attr_native_unit_of_measurement = UnitOfVolume.MILLILITERS
    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, data: ApexRuntimeData, key: str, name: str, abaddr: Any):
        super().__init__(data, key, name)
        self._abaddr = abaddr

    def _module(self) -> dict[str, Any]:
        return _find_module(self.coordinator.data, self._abaddr)

    def _level(self, index: int) -> float | None:
        levels = (self._module().get("extra") or {}).get("levels") or []
        value = _number(levels[index]) if len(levels) > index else None
        return round(value, 1) if value is not None else None

    def _reset_time(self, index: int) -> datetime | None:
        times = (self._module().get("extra") or {}).get("resetTime") or []
        return _epoch_to_datetime(times[index]) if len(times) > index else None

    @property
    def available(self) -> bool:
        return super().available and bool(self._module())


class ApexTridentReagentSensor(ApexTridentSensor):
    """Reagent left in a Trident (estimated by the Apex from the tests run since it was replaced)."""

    _attr_icon = "mdi:flask"

    def __init__(self, data: ApexRuntimeData, abaddr: Any, module_name: str, letter: str, index: int):
        super().__init__(data, f"trident_{abaddr}_reagent_{letter}", f"{module_name} Reagent {letter}", abaddr)
        self._index = index

    @property
    def native_value(self):
        return self._level(self._index)

    @property
    def extra_state_attributes(self):
        ml = self._level(self._index)
        tests = None
        if ml is not None:
            tests = math.floor(max(0.0, min(TRIDENT_MAX_REAGENT_ML, ml)) / trident_ml_per_test(self._module().get("hwtype")))
        replaced = self._reset_time(self._index)
        return {"tests_remaining": tests, "replaced": replaced.isoformat() if replaced else None}


class ApexTridentWasteSensor(ApexTridentSensor):
    """Waste collected by a Trident since the container was emptied."""

    _attr_icon = "mdi:delete-variant"

    def __init__(self, data: ApexRuntimeData, abaddr: Any, module_name: str):
        super().__init__(data, f"trident_{abaddr}_waste", f"{module_name} Waste", abaddr)

    @property
    def native_value(self):
        return self._level(TRIDENT_WASTE)

    @property
    def extra_state_attributes(self):
        used = self._level(TRIDENT_WASTE)
        size = _number((_module_config(self.coordinator.data, self._abaddr).get("extra") or {}).get("wasteSize"))
        emptied = self._reset_time(TRIDENT_WASTE)
        return {
            # size 0: the waste goes to a drain
            "container_ml": size,
            "percent_full": round(used / size * 100, 1) if size and used is not None else None,
            "emptied": emptied.isoformat() if emptied else None,
        }


FEED_OFF = "Off"


class ApexFeedEntity(ApexEntity, SensorEntity):
    """A sensor for the feed cycle (status.json only)."""

    def _feed(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("feed") or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self._feed())


class ApexFeedSensor(ApexFeedEntity):
    """Which feed cycle (A-D) is running, if any."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [FEED_OFF, *FEED_LETTERS.values()]

    def __init__(self, data: ApexRuntimeData):
        super().__init__(data, "feed_mode", "Feed Mode")

    @property
    def native_value(self):
        name = self._feed().get("name")
        if name == 0:
            return FEED_OFF
        # anything else (e.g. a bogus cycle) is unknown
        return FEED_LETTERS.get(name)

    @property
    def icon(self) -> str:
        return "mdi:fish-off" if self.native_value == FEED_OFF else "mdi:fish"

    @property
    def extra_state_attributes(self):
        f = self._feed()
        running = f.get("name") in FEED_LETTERS
        return {
            # seconds left as of the last poll
            "remaining_seconds": f.get("active") if running else 0,
            "apex_feed_name": f.get("name"),
            "apex_feed_active": f.get("active"),
        }


class ApexFeedEndsSensor(ApexFeedEntity):
    """When the running feed cycle ends.

    The Apex only reports seconds remaining as of each poll; as a timestamp the
    frontend can count down between polls ("in 4 minutes").
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-sand"

    # polls land at a different point within each second; ignore that much drift
    _JITTER_SECONDS = 2

    def __init__(self, data: ApexRuntimeData):
        super().__init__(data, "feed_ends", "Feed Ends")
        self._ends: datetime | None = None

    def _update_ends(self) -> None:
        f = self._feed()
        remaining = f.get("active")
        if f.get("name") not in FEED_LETTERS or not isinstance(remaining, int) or remaining <= 0:
            self._ends = None
            return
        ends = dt_now() + timedelta(seconds=remaining)
        if self._ends is None or abs((ends - self._ends).total_seconds()) > self._JITTER_SECONDS:
            self._ends = ends

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._update_ends()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_ends()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> datetime | None:
        return self._ends
