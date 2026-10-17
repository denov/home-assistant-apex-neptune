# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.apex.const import DOMAIN
from custom_components.apex.coordinator import ApexDataUpdateCoordinator
from custom_components.apex.entity import ApexEntity, async_apex_command, find_output
from custom_components.apex.rest import ApexRestClient

# output states that are plain on/off (A = under Apex program control)
BOOLEAN_STATES = ("ON", "OFF", "AON", "AOF")


def _is_on_from_state(state: str | None) -> bool:
    s = (state or "").strip().upper()
    return s in ("ON", "AON")


def output_labels(outlets_now: list[dict[str, Any]]) -> dict[str, str]:
    """Label per output key: the Apex name, plus the output ID when two outputs share a name."""
    counts: dict[str, int] = {}
    for o in outlets_now:
        nm = (o.get("name") or "").strip()
        counts[nm] = counts.get(nm, 0) + 1

    out: dict[str, str] = {}
    for o in outlets_now:
        nm = (o.get("name") or "").strip()
        oid = (o.get("output_id") or "").strip()
        key = oid or nm
        if counts.get(nm, 0) > 1 and oid:
            out[key] = f"{nm} ({oid})"
        else:
            out[key] = nm
    return out


def no_control_reason(read_only: bool) -> str:
    if read_only:
        return "The Apex integration is set to read-only. Turn that off in its options to control outputs."
    return "Controlling the Apex needs its password. Add it in the Apex integration's options."


async def async_setup_entry(hass, entry, async_add_entities):
    ctx = hass.data[DOMAIN][entry.entry_id]
    coordinator: ApexDataUpdateCoordinator = ctx["coordinator"]
    uid_base: str = ctx["uid_base"]
    device: DeviceInfo = ctx["device"]
    rest: ApexRestClient | None = ctx.get("control")
    read_only: bool = ctx.get("read_only", False)

    added: set[str] = set()

    def _add_new():
        outlets_now = (coordinator.data or {}).get("outlets", []) or []
        if not outlets_now:
            return

        lbl = output_labels(outlets_now)
        new_entities: list[ApexOutletSwitch] = []

        for o in outlets_now:
            name = (o.get("name") or "").strip()
            output_id = (o.get("output_id") or "").strip()
            state = (o.get("state") or "").strip().upper()

            # only boolean-like states; the mode select covers every output
            if state not in BOOLEAN_STATES:
                continue

            key = output_id or name
            if not key:
                continue

            unique_id = f"{uid_base}_outlet_{key}".lower()
            if unique_id in added:
                continue

            new_entities.append(
                ApexOutletSwitch(coordinator, device, unique_id, lbl.get(key, name), key, rest, read_only)
            )
            added.add(unique_id)

        if new_entities:
            async_add_entities(new_entities, True)

    _add_new()

    remove = coordinator.async_add_listener(_add_new)
    entry.async_on_unload(remove)


class ApexOutletSwitch(ApexEntity, SwitchEntity):
    """An Apex output. On and off set it to ON or OFF (manual); the mode select can set AUTO."""

    def __init__(
        self,
        coordinator: ApexDataUpdateCoordinator,
        device_info: DeviceInfo,
        unique_id: str,
        name: str,
        key: str,
        rest: ApexRestClient | None,
        read_only: bool,
    ):
        super().__init__(coordinator, device_info, unique_id, name)
        self._key = key
        self._rest = rest
        self._read_only = read_only

        # IMPORTANT:
        # Do NOT set assumed_state. That’s what creates the two icons (separate on/off buttons).

    def _output(self) -> dict[str, Any]:
        return find_output(self.coordinator.data, self._key)

    @property
    def is_on(self) -> bool:
        return _is_on_from_state(self._output().get("state"))

    @property
    def icon(self) -> str:
        # One icon that reflects state
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
        did = self._output().get("device_id")
        if self._rest is None:
            raise HomeAssistantError(no_control_reason(self._read_only))
        if not did:
            raise HomeAssistantError(f"The Apex didn't report a device id for {self.name}")
        await async_apex_command(
            self.coordinator, f"turn {self.name} {mode.lower()}", self._rest.async_set_output(did, mode)
        )

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set("ON")

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set("OFF")
