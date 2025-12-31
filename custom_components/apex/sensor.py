# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from homeassistant.core import callback
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricCurrent, UnitOfEnergy, UnitOfPower
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
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
from custom_components.apex.parsing import build_labels, one_apex_prefix, safe_uid_base
from .sensor_helpers import icon_for_type, units_and_meta
from custom_components.apex.power_math import total_current_amps, total_power_watts


async def async_setup_entry(hass, entry, async_add_entities):
    # Shared coordinator created in __init__.py
    ctx = hass.data[DOMAIN][entry.entry_id]
    coordinator: ApexDataUpdateCoordinator = ctx["coordinator"]
    ip: str = ctx["ip"]

    # Used for Total Power = Total Current * Line Voltage
    line_voltage = float(ctx.get("line_voltage", 120))

    merged = {**entry.data, **entry.options}
    configured_temp_unit = merged.get(CONF_TEMP_UNIT, DEFAULT_TEMP_UNIT)
    amps_diagnostic = bool(merged.get(CONF_AMPS_DIAGNOSTIC, DEFAULT_AMPS_DIAGNOSTIC))

    data = coordinator.data or {}
    meta = data.get("meta", {}) or {}

    serial = (meta.get("serial") or "").strip()
    uid_base = safe_uid_base(serial) or safe_uid_base(ip)

    device = DeviceInfo(
        identifiers={(DOMAIN, uid_base)},
        name=f"Apex @ {ip}",
        manufacturer="Neptune Systems",
        model="Apex",
        serial_number=(serial or None),
        configuration_url=f"http://{ip}",
        sw_version=(meta.get("software") or None),
        hw_version=(meta.get("hardware") or None),
    )

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

        label = one_apex_prefix(labels_now.get(probe_name) or probe_name)
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
        label = one_apex_prefix(f"Power {kind.capitalize()}")
        unique_key = f"power_{kind}"
        return ApexTextSensor(
            coordinator=coordinator,
            uid_base=uid_base,
            device_info=device,
            unique_key=unique_key,
            label=label,
        )

    async def _add_new_entities_from_current_data():
        async with add_lock:
            d = coordinator.data or {}
            probes_now = d.get("probes", {}) or {}
            labels_now = build_labels(probes_now, overrides)

            new_entities: list[SensorEntity] = []

            # Probes (auto-discovered)
            for probe_name in sorted(probes_now.keys()):
                uid = f"{uid_base}_{probe_name}".lower()
                if uid in added_uids:
                    continue
                new_entities.append(_make_probe_entity(probe_name, probes_now, labels_now))
                added_uids.add(uid)

            # Power timestamps (appear only if present; will be added later if they show up)
            power = d.get("power") or {}
            failed = power.get("failed")
            restored = power.get("restored")

            if failed:
                uid = f"{uid_base}_power_failed".lower()
                if uid not in added_uids:
                    new_entities.append(_make_power_entity("failed"))
                    added_uids.add(uid)

            if restored:
                uid = f"{uid_base}_power_restored".lower()
                if uid not in added_uids:
                    new_entities.append(_make_power_entity("restored"))
                    added_uids.add(uid)

            if new_entities:
                async_add_entities(new_entities, True)

    await _add_new_entities_from_current_data()

    def _on_coordinator_update():
        hass.async_create_task(_add_new_entities_from_current_data())

    remove_listener = coordinator.async_add_listener(_on_coordinator_update)
    entry.async_on_unload(remove_listener)


class ApexProbeSensor(CoordinatorEntity[ApexDataUpdateCoordinator], SensorEntity):
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
        super().__init__(coordinator)
        self._probe = probe_name
        self._probe_type = probe_type

        self._attr_name = label
        self._attr_unique_id = f"{uid_base}_{probe_name}".lower()
        self._attr_device_info = device_info

        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class

        if self._probe_type.strip().lower() == "amps" and amps_diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        probes = (self.coordinator.data or {}).get("probes", {}) or {}
        p = probes.get(self._probe) or {}
        return p.get("value")

    @property
    def icon(self) -> str | None:
        return icon_for_type(self._probe_type, self._probe)

    @property
    def available(self) -> bool:
        # don't flip everything "unavailable" due to one failed refresh
        return self.coordinator.data is not None

    @property
    def extra_state_attributes(self):
        err = self.coordinator.last_exception
        return {
            "apex_probe_name": self._probe,
            "apex_probe_type": self._probe_type,
            "last_error": (str(err) if err else None),
        }


class ApexTextSensor(CoordinatorEntity[ApexDataUpdateCoordinator], SensorEntity):
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
        super().__init__(coordinator)
        self._key = unique_key

        self._attr_name = label
        self._attr_unique_id = f"{uid_base}_{unique_key}".lower()
        self._attr_device_info = device_info

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

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None


class ApexTotalCurrentSensor(CoordinatorEntity[ApexDataUpdateCoordinator], SensorEntity):
    def __init__(self, coordinator: ApexDataUpdateCoordinator, uid_base: str, device_info: DeviceInfo):
        super().__init__(coordinator)
        self._attr_name = "Apex Total Current"
        self._attr_unique_id = f"{uid_base}_total_current"
        self._attr_device_info = device_info

        self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
        self._attr_device_class = SensorDeviceClass.CURRENT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:current-ac"

    @property
    def native_value(self):
        probes = (self.coordinator.data or {}).get("probes", {}) or {}
        amps = total_current_amps(probes)
        return round(amps, 3) if amps is not None else None

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None


class ApexTotalPowerSensor(CoordinatorEntity[ApexDataUpdateCoordinator], SensorEntity):
    def __init__(self, coordinator: ApexDataUpdateCoordinator, uid_base: str, device_info: DeviceInfo, *, line_voltage: float):
        super().__init__(coordinator)
        self._v = float(line_voltage)

        self._attr_name = "Apex Total Power"
        self._attr_unique_id = f"{uid_base}_total_power"
        self._attr_device_info = device_info

        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:flash"

    @property
    def native_value(self):
        probes = (self.coordinator.data or {}).get("probes", {}) or {}
        watts = total_power_watts(probes, self._v)
        return round(watts, 1) if watts is not None else None

    @property
    def extra_state_attributes(self):
        return {"line_voltage": self._v}

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None


class ApexTotalEnergySensor(CoordinatorEntity[ApexDataUpdateCoordinator], RestoreEntity, SensorEntity):
    """Accumulates kWh from total power readings (W) over time."""

    _attr_name = "Apex Total Energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: ApexDataUpdateCoordinator, uid_base: str, device_info: DeviceInfo):
        super().__init__(coordinator)
        self._attr_unique_id = f"{uid_base}_total_energy"
        self._attr_device_info = device_info

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

    def _current_total_watts(self) -> float | None:
        probes = (self.coordinator.data or {}).get("probes", {}) or {}

        line_v = 120.0
        if hasattr(self.coordinator, "line_voltage"):
            try:
                line_v = float(getattr(self.coordinator, "line_voltage"))
            except Exception:
                pass

        return total_power_watts(probes, line_v)

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

        watts = self._current_total_watts()
        if watts is None:
            self.async_write_ha_state()
            return

        self._kwh += (watts * (dt_seconds / 3600.0)) / 1000.0
        self.async_write_ha_state()

    @property
    def native_value(self):
        return round(self._kwh or 0.0, 6)

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None
