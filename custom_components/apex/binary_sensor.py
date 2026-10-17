# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.apex.const import DOMAIN
from custom_components.apex.coordinator import ApexDataUpdateCoordinator
from custom_components.apex.entity import ApexEntity

# a status[2] that isn't an error (DOS and pumps report e.g. "Volt" there when something is wrong)
OUTPUT_OK = (None, "", "OK")


async def async_setup_entry(hass, entry, async_add_entities):
    ctx = hass.data[DOMAIN][entry.entry_id]
    coordinator: ApexDataUpdateCoordinator = ctx["coordinator"]
    uid_base: str = ctx["uid_base"]
    device: DeviceInfo = ctx["device"]

    added: set[str] = set()

    def _add_new():
        data = coordinator.data or {}
        inputs_now = data.get("inputs", {}) or {}
        new_entities: list[BinarySensorEntity] = []

        for did in sorted(inputs_now):
            unique_id = f"{uid_base}_input_{did}".lower()
            if unique_id in added:
                continue
            label = inputs_now[did].get("name") or did
            new_entities.append(ApexDigitalInput(coordinator, device, unique_id, label, did))
            added.add(unique_id)

        # status.json reports output types; status.xml doesn't, so no alert outputs to watch there
        alarm = ApexAlarm(coordinator, device, f"{uid_base}_alarm")
        if alarm.unique_id not in added and any(o.get("type") for o in data.get("outlets", []) or []):
            new_entities.append(alarm)
            added.add(alarm.unique_id)

        if new_entities:
            # coordinator data is already fresh; update_before_add would poll the Apex again
            async_add_entities(new_entities)

    _add_new()

    remove = coordinator.async_add_listener(_add_new)
    entry.async_on_unload(remove)


class ApexDigitalInput(ApexEntity, BinarySensorEntity):
    """Apex switch input (float, optical, leak, ...). On means the circuit is CLOSED.

    No device class: what CLOSED means depends on the sensor and how it's mounted, so users
    pick one (moisture, problem, ...) and can invert it with a template if needed.
    """

    def __init__(
        self,
        coordinator: ApexDataUpdateCoordinator,
        device_info: DeviceInfo,
        unique_id: str,
        name: str,
        did: str,
    ):
        super().__init__(coordinator, device_info, unique_id, name)
        self._did = did

    def _input(self) -> dict:
        return ((self.coordinator.data or {}).get("inputs", {}) or {}).get(self._did) or {}

    @property
    def is_on(self) -> bool | None:
        state = self._input().get("state")
        if state is None:
            return None
        return state == "CLOSED"

    @property
    def icon(self) -> str:
        return "mdi:electric-switch-closed" if self.is_on else "mdi:electric-switch"

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None and bool(self._input())

    @property
    def extra_state_attributes(self):
        i = self._input()
        return {
            "apex_input_name": i.get("name"),
            "apex_device_id": self._did,
            "apex_state": i.get("state"),
            "raw_value": i.get("value"),
        }


class ApexAlarm(ApexEntity, BinarySensorEntity):
    """On while any Apex alert output (email alarm, sound alarm, an FMM alarm, ...) is on.

    The Apex doesn't say why it's alarming; the attributes list the alert outputs that are on
    and outputs reporting an error (e.g. a DOS or pump with "Volt").
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ApexDataUpdateCoordinator, device_info: DeviceInfo, unique_id: str):
        super().__init__(coordinator, device_info, unique_id, "Alarm")

    def _outputs(self) -> list[dict[str, Any]]:
        return (self.coordinator.data or {}).get("outlets", []) or []

    def _active_alerts(self) -> list[str]:
        return [
            o["name"]
            for o in self._outputs()
            if (o.get("type") or "").lower() == "alert" and (o.get("state") or "").upper() in ("ON", "AON")
        ]

    def _output_errors(self) -> dict[str, str]:
        errors: dict[str, str] = {}
        for o in self._outputs():
            status = o.get("status") or []
            if len(status) > 2 and status[2] not in OUTPUT_OK:
                errors[o["name"]] = status[2]
        return errors

    @property
    def is_on(self) -> bool:
        return bool(self._active_alerts())

    @property
    def extra_state_attributes(self):
        return {"active_alerts": self._active_alerts(), "output_errors": self._output_errors()}
