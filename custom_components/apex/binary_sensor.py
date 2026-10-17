# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import ApexConfigEntry, ApexEntity, ApexRuntimeData, async_add_discovered, is_on_state

# a status[2] that isn't an error (DOS and pumps report e.g. "Volt" there when something is wrong)
OUTPUT_OK = (None, "", "OK")


async def async_setup_entry(hass, entry: ApexConfigEntry, async_add_entities):
    data = entry.runtime_data

    def discover(d: dict[str, Any]):
        inputs = d.get("inputs", {}) or {}
        for did in sorted(inputs):
            yield f"input_{did}", lambda did=did: ApexDigitalInput(data, did, inputs[did].get("name") or did)
        # status.json reports output types; status.xml doesn't, so no alert outputs to watch there
        if any(o.get("type") for o in d.get("outlets", []) or []):
            yield "alarm", lambda: ApexAlarm(data)

    async_add_discovered(entry, async_add_entities, discover)


class ApexDigitalInput(ApexEntity, BinarySensorEntity):
    """Apex switch input (float, optical, leak, ...). On means the circuit is CLOSED.

    No device class: what CLOSED means depends on the sensor and how it's mounted, so users
    pick one (moisture, problem, ...) and can invert it with a template if needed.
    """

    def __init__(self, data: ApexRuntimeData, did: str, name: str):
        super().__init__(data, f"input_{did}", name)
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
        return super().available and bool(self._input())

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

    def __init__(self, data: ApexRuntimeData):
        super().__init__(data, "alarm", "Alarm")

    def _outputs(self) -> list[dict[str, Any]]:
        return (self.coordinator.data or {}).get("outlets", []) or []

    def _active_alerts(self) -> list[str]:
        return [
            o["name"]
            for o in self._outputs()
            if (o.get("type") or "").lower() == "alert" and is_on_state(o.get("state"))
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
