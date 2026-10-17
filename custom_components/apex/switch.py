# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .entity import (
    ApexConfigEntry,
    ApexOutputEntity,
    ApexRuntimeData,
    async_add_discovered,
    async_apex_command,
    is_on_state,
    no_control_reason,
    output_key,
    output_labels,
)

# output states that are plain on/off (A = under Apex program control)
BOOLEAN_STATES = ("ON", "OFF", "AON", "AOF")


async def async_setup_entry(hass, entry: ApexConfigEntry, async_add_entities):
    data = entry.runtime_data

    def discover(d: dict[str, Any]):
        outlets = d.get("outlets", []) or []
        labels = output_labels(outlets)
        for o in outlets:
            key = output_key(o)
            # only boolean-like states; the mode select covers every output
            if key and (o.get("state") or "").strip().upper() in BOOLEAN_STATES:
                yield key, lambda key=key: ApexOutletSwitch(data, key, labels.get(key) or key)

    async_add_discovered(entry, async_add_entities, discover)


class ApexOutletSwitch(ApexOutputEntity, SwitchEntity):
    """An Apex output. On and off set it to ON or OFF (manual); the mode select can set AUTO.

    No assumed_state: that would show separate on and off buttons.
    """

    def __init__(self, data: ApexRuntimeData, key: str, name: str):
        super().__init__(data, key, f"outlet_{key}", name)
        self._rest = data.control
        self._read_only = data.settings.read_only

    @property
    def is_on(self) -> bool:
        return is_on_state(self._output().get("state"))

    @property
    def icon(self) -> str:
        return "mdi:power-plug" if self.is_on else "mdi:power-plug-off"

    @property
    def extra_state_attributes(self):
        o = self._output()
        if not o:
            return {}
        return {
            "outlet_name": o.get("name"),
            "output_id": o.get("output_id"),
            "device_id": o.get("device_id"),
            "raw_state": o.get("state"),
            "apex_type": o.get("type"),
            "raw_status": o.get("status"),
        }

    async def _async_set(self, mode: str) -> None:
        if self._rest is None:
            raise HomeAssistantError(no_control_reason(self._read_only))
        await async_apex_command(
            self.coordinator, f"turn {self.name} {mode.lower()}", self._rest.async_set_output(self._did(), mode)
        )

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set("ON")

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set("OFF")
