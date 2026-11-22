# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import (
    ApexConfigEntry,
    ApexEntity,
    ApexRuntimeData,
    async_add_discovered,
    find_module,
    is_on_state,
    module_device,
)

# a status[2] that isn't an error (DOS and pumps report e.g. "Volt" there when something is wrong)
OUTPUT_OK = (None, "", "OK")

# status.json link.linkState, as the Apex dashboard shows it; anything else is "no communication"
FUSION_LINK_STATES = {1: "not_linked", 2: "linking", 3: "connected"}
FUSION_CONNECTED = 3


async def async_setup_entry(hass, entry: ApexConfigEntry, async_add_entities):
    data = entry.runtime_data

    def discover(d: dict[str, Any]):
        inputs = d.get("inputs", {}) or {}
        for did in sorted(inputs):
            yield f"input_{did}", lambda did=did: ApexDigitalInput(data, did, inputs[did].get("name") or did)
        # status.json reports output types; status.xml doesn't, so no alert outputs to watch there
        if any(o.get("type") for o in d.get("outlets", []) or []):
            yield "alarm", lambda: ApexAlarm(data)
        # status.json only
        if d.get("link") is not None:
            yield "fusion", lambda: ApexFusionLink(data)
        # AquaBus modules (REST status, so only with a password)
        for m in d.get("modules", []) or []:
            if isinstance(m.get("abaddr"), int):
                yield f"module_{m['abaddr']}", lambda m=m: ApexModuleConnected(data, m["abaddr"])

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


class ApexFusionLink(ApexEntity, BinarySensorEntity):
    """On while the Apex is connected to Apex Fusion (what the Apex dashboard shows as OK)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-outline"

    def __init__(self, data: ApexRuntimeData):
        super().__init__(data, "fusion", "Fusion")

    def _state(self) -> int | None:
        return ((self.coordinator.data or {}).get("link") or {}).get("state")

    @property
    def available(self) -> bool:
        return super().available and self._state() is not None

    @property
    def is_on(self) -> bool:
        return self._state() == FUSION_CONNECTED

    @property
    def extra_state_attributes(self):
        state = self._state()
        return {"link_state": FUSION_LINK_STATES.get(state, "no_communication"), "apex_link_state": state}


class ApexModuleConnected(ApexEntity, BinarySensorEntity):
    """On while an AquaBus module is connected to the Apex. Read every few minutes, with the REST status."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, data: ApexRuntimeData, abaddr: int):
        super().__init__(data, f"module_{abaddr}_connected", "Connected", module_device(data, abaddr))
        self._abaddr = abaddr

    def _module(self) -> dict[str, Any]:
        return find_module(self.coordinator.data, self._abaddr)

    @property
    def available(self) -> bool:
        return super().available and bool(self._module())

    @property
    def is_on(self) -> bool:
        return self._module().get("present") is True

    @property
    def extra_state_attributes(self):
        m = self._module()
        return {
            "aquabus_address": self._abaddr,
            "hardware_type": m.get("hwtype"),
            # "OLD" when the module's firmware is older than the Apex expects
            "firmware_status": m.get("swstat"),
            "firmware_version": m.get("swrev"),
        }
