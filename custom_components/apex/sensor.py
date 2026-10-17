# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
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
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.dt import now as dt_now

from custom_components.apex.const import (
    DOMAIN,
    SENSOR_MAP,  # optional overrides: { "ProbeName": ("Friendly Name", unit_or_None) }
    CONF_TEMP_UNIT,
    DEFAULT_TEMP_UNIT,
    CONF_AMPS_DIAGNOSTIC,
    DEFAULT_AMPS_DIAGNOSTIC,
)
from custom_components.apex.coordinator import ApexDataUpdateCoordinator
from custom_components.apex.entity import ApexEntity, find_output, find_output_config
from custom_components.apex.parsing import build_labels
from .sensor_helpers import icon_for_type, units_and_meta
from custom_components.apex.power_math import total_current_amps, total_power_watts
from custom_components.apex.rest import FEED_LETTERS

DOS_TYPES = ("dos", "dqd")
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


def output_key(o: dict[str, Any]) -> str:
    return (o.get("output_id") or "").strip() or (o.get("name") or "").strip()


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


def _module_name(data: dict[str, Any], module: dict[str, Any]) -> str:
    config = data.get("config") or {}
    for m in config.get("mconf") or []:
        if isinstance(m, dict) and m.get("abaddr") == module.get("abaddr") and m.get("name"):
            return str(m["name"])
    return f"{module.get('hwtype')}_{module.get('abaddr')}"


def _module_config(data: dict[str, Any] | None, abaddr: Any) -> dict[str, Any]:
    config = (data or {}).get("config") or {}
    for m in config.get("mconf") or []:
        if isinstance(m, dict) and m.get("abaddr") == abaddr:
            return m
    return {}


def _find_module(data: dict[str, Any] | None, abaddr: Any) -> dict[str, Any]:
    for m in (data or {}).get("modules", []) or []:
        if m.get("abaddr") == abaddr:
            return m
    return {}


def _epoch_to_datetime(epoch: Any) -> datetime | None:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool) or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc)


async def async_setup_entry(hass, entry, async_add_entities):
    # Shared coordinator created in __init__.py
    ctx = hass.data[DOMAIN][entry.entry_id]
    coordinator: ApexDataUpdateCoordinator = ctx["coordinator"]
    uid_base: str = ctx["uid_base"]
    device: DeviceInfo = ctx["device"]

    # Used for Total Power = Total Current * Line Voltage
    line_voltage = float(ctx.get("line_voltage", 120))
    per_outlet_energy = bool(ctx.get("enable_per_outlet_power"))

    merged = {**entry.data, **entry.options}
    configured_temp_unit = merged.get(CONF_TEMP_UNIT, DEFAULT_TEMP_UNIT)
    amps_diagnostic = bool(merged.get(CONF_AMPS_DIAGNOSTIC, DEFAULT_AMPS_DIAGNOSTIC))

    overrides: dict[str, tuple[str, str | None]] = SENSOR_MAP or {}

    # Track by unique_id so we never re-add on refresh
    added_uids: set[str] = set()
    add_lock = asyncio.Lock()

    total_current = ApexTotalCurrentSensor(coordinator, uid_base, device)
    total_power = ApexTotalPowerSensor(coordinator, uid_base, device, line_voltage=line_voltage)
    total_energy = ApexTotalEnergySensor(coordinator, uid_base, device)

    async_add_entities([total_current, total_power, total_energy], True)
    added_uids.add(total_current.unique_id)
    added_uids.add(total_power.unique_id)
    added_uids.add(total_energy.unique_id)

    def _make_probe_entity(
        probe_name: str,
        probes_now: dict[str, dict[str, Any]],
        labels_now: dict[str, str],
    ) -> ApexProbeSensor:
        p = probes_now.get(probe_name) or {}
        typ = (p.get("type") or "").strip()
        val = p.get("value")

        label = labels_now.get(probe_name) or probe_name
        unit, device_class, state_class = units_and_meta(
            probe_name, typ, val, overrides, configured_temp_unit
        )

        return ApexProbeSensor(
            coordinator=coordinator,
            uid_base=uid_base,
            device_info=device,
            probe_name=probe_name,
            probe_type=typ,
            label=label,
            unit=unit,
            device_class=device_class,
            state_class=state_class,
            amps_diagnostic=amps_diagnostic,
        )

    def _make_power_entity(kind: str) -> ApexTextSensor:
        # kind: "failed" or "restored"
        return ApexTextSensor(
            coordinator=coordinator,
            uid_base=uid_base,
            device_info=device,
            unique_key=f"power_{kind}",
            label=f"Power {kind.capitalize()}",
        )

    def _output_entities(d: dict[str, Any]) -> list[SensorEntity]:
        entities: list[SensorEntity] = []
        for o in d.get("outlets", []) or []:
            key = output_key(o)
            if not key:
                continue
            typ = (o.get("type") or "").lower()
            name = o.get("name") or key
            if typ in DOS_TYPES and dos_volumes(o)[1] is not None:
                entities.append(ApexDosVolumeSensor(coordinator, uid_base, device, key, name))
            elif output_intensity(o) is not None:
                entities.append(ApexIntensitySensor(coordinator, uid_base, device, key, name))
            if typ in VARIABLE_TYPES and find_output_config(d, o.get("device_id")):
                entities.append(ApexSettingSensor(coordinator, uid_base, device, key, name))
        return entities

    def _trident_entities(d: dict[str, Any]) -> list[SensorEntity]:
        entities: list[SensorEntity] = []
        for m in d.get("modules", []) or []:
            hwtype = m.get("hwtype")
            levels = (m.get("extra") or {}).get("levels")
            if hwtype not in TRIDENT_TYPES or not isinstance(levels, list) or len(levels) < 5:
                continue
            module_name = _module_name(d, m)
            for letter, index in zip(trident_reagent_names(hwtype), TRIDENT_REAGENTS):
                entities.append(
                    ApexTridentReagentSensor(coordinator, uid_base, device, m["abaddr"], module_name, letter, index)
                )
            entities.append(ApexTridentWasteSensor(coordinator, uid_base, device, m["abaddr"], module_name))
        return entities

    async def _add_new_entities_from_current_data():
        async with add_lock:
            d = coordinator.data or {}
            probes_now = d.get("probes", {}) or {}
            labels_now = build_labels(probes_now, overrides)

            new_entities: list[SensorEntity] = []

            # Probes (auto-discovered)
            for probe_name in sorted(probes_now.keys()):
                if f"{uid_base}_{probe_name}".lower() not in added_uids:
                    new_entities.append(_make_probe_entity(probe_name, probes_now, labels_now))

            # Energy for each outlet the Apex measures the power of (EB832)
            if per_outlet_energy:
                for probe_name, p in probes_now.items():
                    if (p.get("type") or "").strip().lower() != "pwr":
                        continue
                    label = f"{labels_now.get(probe_name) or probe_name} Energy"
                    new_entities.append(ApexProbeEnergySensor(coordinator, uid_base, device, probe_name, label))

            # Power timestamps (appear only if present; will be added later if they show up)
            power = d.get("power") or {}
            if power.get("failed"):
                new_entities.append(_make_power_entity("failed"))
            if power.get("restored"):
                new_entities.append(_make_power_entity("restored"))

            # Feed cycle (status.json only)
            if d.get("feed") is not None:
                new_entities.append(ApexFeedSensor(coordinator, uid_base, device))
                new_entities.append(ApexFeedEndsSensor(coordinator, uid_base, device))

            new_entities.extend(_output_entities(d))
            # Trident levels (REST status, so only with a password)
            new_entities.extend(_trident_entities(d))

            fresh = [e for e in new_entities if e.unique_id not in added_uids]
            added_uids.update(e.unique_id for e in fresh)
            if fresh:
                async_add_entities(fresh, True)

    await _add_new_entities_from_current_data()

    def _on_coordinator_update():
        hass.async_create_task(_add_new_entities_from_current_data())

    remove_listener = coordinator.async_add_listener(_on_coordinator_update)
    entry.async_on_unload(remove_listener)


class ApexProbeSensor(ApexEntity, SensorEntity):
    def __init__(
        self,
        *,
        coordinator: ApexDataUpdateCoordinator,
        uid_base: str,
        device_info: DeviceInfo,
        probe_name: str,
        probe_type: str,
        label: str,
        unit: str | None,
        device_class: SensorDeviceClass | None,
        state_class: SensorStateClass | None,
        amps_diagnostic: bool,
    ):
        super().__init__(coordinator, device_info, f"{uid_base}_{probe_name}".lower(), label)
        self._probe = probe_name
        self._probe_type = probe_type

        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class

        if self._probe_type.strip().lower() == "amps" and amps_diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _p(self) -> dict[str, Any]:
        probes = (self.coordinator.data or {}).get("probes", {}) or {}
        return probes.get(self._probe) or {}

    @property
    def native_value(self):
        return self._p().get("value")

    @property
    def icon(self) -> str | None:
        return icon_for_type(self._probe_type, self._probe)

    @property
    def extra_state_attributes(self):
        err = self.coordinator.last_exception
        return {
            "apex_probe_name": self._p().get("name") or self._probe,
            "apex_probe_type": self._probe_type,
            "last_error": (str(err) if err else None),
        }


class ApexTextSensor(ApexEntity, SensorEntity):
    """Diagnostic text sensor for power failed/restored timestamps."""
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        *,
        coordinator: ApexDataUpdateCoordinator,
        uid_base: str,
        device_info: DeviceInfo,
        unique_key: str,
        label: str,
    ):
        super().__init__(coordinator, device_info, f"{uid_base}_{unique_key}".lower(), label)
        self._key = unique_key
        self._attr_icon = "mdi:power-plug-off" if "failed" in unique_key else "mdi:power-plug"

    @property
    def native_value(self):
        d = self.coordinator.data or {}
        power = d.get("power") or {}

        if self._key == "power_failed":
            return power.get("failed")
        if self._key == "power_restored":
            return power.get("restored")
        return None


class ApexTotalCurrentSensor(ApexEntity, SensorEntity):
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:current-ac"

    def __init__(self, coordinator: ApexDataUpdateCoordinator, uid_base: str, device_info: DeviceInfo):
        super().__init__(coordinator, device_info, f"{uid_base}_total_current", "Total Current")

    @property
    def native_value(self):
        probes = (self.coordinator.data or {}).get("probes", {}) or {}
        amps = total_current_amps(probes)
        return round(amps, 3) if amps is not None else None


class ApexTotalPowerSensor(ApexEntity, SensorEntity):
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    def __init__(self, coordinator: ApexDataUpdateCoordinator, uid_base: str, device_info: DeviceInfo, *, line_voltage: float):
        super().__init__(coordinator, device_info, f"{uid_base}_total_power", "Total Power")
        self._v = float(line_voltage)

    @property
    def native_value(self):
        probes = (self.coordinator.data or {}).get("probes", {}) or {}
        watts = total_power_watts(probes, self._v)
        return round(watts, 1) if watts is not None else None

    @property
    def extra_state_attributes(self):
        return {"line_voltage": self._v}


class ApexEnergySensor(ApexEntity, RestoreEntity, SensorEntity):
    """Accumulates kWh from power readings (W) over time."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: ApexDataUpdateCoordinator, device_info: DeviceInfo, unique_id: str, name: str):
        super().__init__(coordinator, device_info, unique_id, name)
        self._kwh: float | None = None
        self._last_ts: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        state = await self.async_get_last_state()
        if state and state.state not in (None, "unknown", "unavailable"):
            try:
                self._kwh = float(state.state)
            except Exception:
                self._kwh = 0.0
        else:
            self._kwh = 0.0

        self._last_ts = dt_now()

    def _current_watts(self) -> float | None:
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._kwh is None:
            self._kwh = 0.0

        now_ts = dt_now()
        if self._last_ts is None:
            self._last_ts = now_ts
            self.async_write_ha_state()
            return

        dt_seconds = (now_ts - self._last_ts).total_seconds()
        self._last_ts = now_ts

        if dt_seconds <= 0 or dt_seconds > 3600:
            self.async_write_ha_state()
            return

        watts = self._current_watts()
        if watts is None:
            self.async_write_ha_state()
            return

        self._kwh += (watts * (dt_seconds / 3600.0)) / 1000.0
        self.async_write_ha_state()

    @property
    def native_value(self):
        return round(self._kwh or 0.0, 6)


class ApexTotalEnergySensor(ApexEnergySensor):
    """Energy from Total Power (all Amps probes x line voltage)."""

    def __init__(self, coordinator: ApexDataUpdateCoordinator, uid_base: str, device_info: DeviceInfo):
        super().__init__(coordinator, device_info, f"{uid_base}_total_energy", "Total Energy")

    def _current_watts(self) -> float | None:
        probes = (self.coordinator.data or {}).get("probes", {}) or {}

        line_v = 120.0
        if hasattr(self.coordinator, "line_voltage"):
            try:
                line_v = float(getattr(self.coordinator, "line_voltage"))
            except Exception:
                pass

        return total_power_watts(probes, line_v)


class ApexProbeEnergySensor(ApexEnergySensor):
    """Energy from one outlet's power probe (EB832 "pwr" inputs)."""

    def __init__(
        self, coordinator: ApexDataUpdateCoordinator, uid_base: str, device_info: DeviceInfo, probe_name: str, label: str
    ):
        super().__init__(coordinator, device_info, f"{uid_base}_{probe_name}_energy".lower(), label)
        self._probe = probe_name

    def _current_watts(self) -> float | None:
        probes = (self.coordinator.data or {}).get("probes", {}) or {}
        value = (probes.get(self._probe) or {}).get("value")
        return float(value) if isinstance(value, (int, float)) else None


class ApexOutputSensor(ApexEntity, SensorEntity):
    """A reading from an output's status (keyed like the outlet switch: output ID, else name)."""

    def __init__(
        self,
        coordinator: ApexDataUpdateCoordinator,
        uid_base: str,
        device_info: DeviceInfo,
        key: str,
        kind: str,
        name: str,
    ):
        super().__init__(coordinator, device_info, f"{uid_base}_output_{key}_{kind}".lower(), name)
        self._key = key

    def _output(self) -> dict[str, Any]:
        return find_output(self.coordinator.data, self._key)

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None and bool(self._output())


class ApexDosVolumeSensor(ApexOutputSensor):
    """Volume left in a DOS/DQD reservoir, as the Apex counts it down from the configured size."""

    _attr_native_unit_of_measurement = UnitOfVolume.MILLILITERS
    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cup-water"

    def __init__(self, coordinator, uid_base, device_info, key: str, output_name: str):
        super().__init__(coordinator, uid_base, device_info, key, "volume_left", f"{output_name} Volume Left")

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

    def __init__(self, coordinator, uid_base, device_info, key: str, output_name: str):
        super().__init__(coordinator, uid_base, device_info, key, "intensity", f"{output_name} Intensity")

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

    def __init__(self, coordinator, uid_base, device_info, key: str, output_name: str):
        super().__init__(coordinator, uid_base, device_info, key, "setting", f"{output_name} Setting")

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

    def __init__(self, coordinator, device_info, unique_id: str, name: str, abaddr: Any):
        super().__init__(coordinator, device_info, unique_id, name)
        self._abaddr = abaddr

    def _module(self) -> dict[str, Any]:
        return _find_module(self.coordinator.data, self._abaddr)

    def _level(self, index: int) -> float | None:
        levels = (self._module().get("extra") or {}).get("levels") or []
        value = levels[index] if len(levels) > index else None
        return round(float(value), 1) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _reset_time(self, index: int) -> datetime | None:
        times = (self._module().get("extra") or {}).get("resetTime") or []
        return _epoch_to_datetime(times[index]) if len(times) > index else None

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None and bool(self._module())


class ApexTridentReagentSensor(ApexTridentSensor):
    """Reagent left in a Trident (estimated by the Apex from the tests run since it was replaced)."""

    _attr_icon = "mdi:flask"

    def __init__(self, coordinator, uid_base, device_info, abaddr: Any, module_name: str, letter: str, index: int):
        super().__init__(
            coordinator,
            device_info,
            f"{uid_base}_trident_{abaddr}_reagent_{letter}".lower(),
            f"{module_name} Reagent {letter}",
            abaddr,
        )
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

    def __init__(self, coordinator, uid_base, device_info, abaddr: Any, module_name: str):
        super().__init__(
            coordinator, device_info, f"{uid_base}_trident_{abaddr}_waste".lower(), f"{module_name} Waste", abaddr
        )

    @property
    def native_value(self):
        return self._level(TRIDENT_WASTE)

    @property
    def extra_state_attributes(self):
        used = self._level(TRIDENT_WASTE)
        size = (_module_config(self.coordinator.data, self._abaddr).get("extra") or {}).get("wasteSize")
        size = float(size) if isinstance(size, (int, float)) and not isinstance(size, bool) else None
        emptied = self._reset_time(TRIDENT_WASTE)
        return {
            # size 0: the waste goes to a drain
            "container_ml": size,
            "percent_full": round(used / size * 100, 1) if size and used is not None else None,
            "emptied": emptied.isoformat() if emptied else None,
        }


FEED_OFF = "Off"


class ApexFeedSensor(ApexEntity, SensorEntity):
    """Which feed cycle (A-D) is running, if any."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [FEED_OFF, *FEED_LETTERS.values()]

    def __init__(self, coordinator: ApexDataUpdateCoordinator, uid_base: str, device_info: DeviceInfo):
        super().__init__(coordinator, device_info, f"{uid_base}_feed_mode", "Feed Mode")

    def _feed(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("feed") or {}

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
    def available(self) -> bool:
        return self.coordinator.data is not None and bool(self._feed())

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


class ApexFeedEndsSensor(ApexEntity, SensorEntity):
    """When the running feed cycle ends.

    The Apex only reports seconds remaining as of each poll; as a timestamp the
    frontend can count down between polls ("in 4 minutes").
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-sand"

    # polls land at a different point within each second; ignore that much drift
    _JITTER_SECONDS = 2

    def __init__(self, coordinator: ApexDataUpdateCoordinator, uid_base: str, device_info: DeviceInfo):
        super().__init__(coordinator, device_info, f"{uid_base}_feed_ends", "Feed Ends")
        self._ends: datetime | None = None

    def _feed(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("feed") or {}

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

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None and bool(self._feed())
