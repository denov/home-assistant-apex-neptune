# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import re
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.apex.const import DOMAIN
from custom_components.apex.coordinator import ApexDataUpdateCoordinator


def _safe_uid_base(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (s or "").lower()).strip("_")


def _one_apex_prefix(name: str) -> str:
    raw = (name or "").strip()
    if raw.lower().startswith("apex "):
        raw = raw[5:].lstrip()
    return f"Apex {raw}"


def _is_on_from_state(state: str | None) -> bool:
    s = (state or "").strip().upper()
    return s in ("ON", "AON")


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: ApexDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    ip = hass.data[DOMAIN][entry.entry_id]["ip"]

    data = coordinator.data or {}
    meta = data.get("meta", {}) or {}
    outlets = data.get("outlets", []) or []

    serial = (meta.get("serial") or "").strip()
    uid_base = _safe_uid_base(serial) or _safe_uid_base(ip)

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

    added: set[str] = set()

    def _labels(outlets_now: list[dict[str, Any]]) -> dict[str, str]:
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

    def _add_new():
        outlets_now = (coordinator.data or {}).get("outlets", []) or []
        if not outlets_now:
            return

        lbl = _labels(outlets_now)
        new_entities: list[ApexOutletSwitch] = []

        for o in outlets_now:
            name = (o.get("name") or "").strip()
            output_id = (o.get("output_id") or "").strip()
            state = (o.get("state") or "").strip().upper()

            # only boolean-like states
            if state not in ("ON", "OFF", "AON", "AOF"):
                continue

            key = output_id or name
            if not key:
                continue

            unique_id = f"{uid_base}_outlet_{key}".lower()
            if unique_id in added:
                continue

            label = _one_apex_prefix(lbl.get(key, name))
            new_entities.append(ApexOutletSwitch(coordinator, device, unique_id, label, key))
            added.add(unique_id)

        if new_entities:
            async_add_entities(new_entities, True)

    _add_new()

    def _on_update():
        _add_new()

    remove = coordinator.async_add_listener(_on_update)
    entry.async_on_unload(remove)


class ApexOutletSwitch(CoordinatorEntity[ApexDataUpdateCoordinator], SwitchEntity):
    """Read-only outlet switch (state only)."""

    def __init__(
        self,
        coordinator: ApexDataUpdateCoordinator,
        device_info: DeviceInfo,
        unique_id: str,
        name: str,
        key: str,
    ):
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_device_info = device_info

        # IMPORTANT:
        # Do NOT set assumed_state. That’s what creates the two icons (separate on/off buttons).

    @property
    def is_on(self) -> bool:
        outlets = (self.coordinator.data or {}).get("outlets", []) or []
        for o in outlets:
            key = ((o.get("output_id") or "").strip() or (o.get("name") or "").strip())
            if key == self._key:
                return _is_on_from_state(o.get("state"))
        return False

    @property
    def icon(self) -> str:
        # One icon that reflects state
        return "mdi:power-plug" if self.is_on else "mdi:power-plug-off"

    @property
    def extra_state_attributes(self):
        outlets = (self.coordinator.data or {}).get("outlets", []) or []
        for o in outlets:
            key = ((o.get("output_id") or "").strip() or (o.get("name") or "").strip())
            if key == self._key:
                return {
                    "outlet_name": o.get("name"),
                    "output_id": o.get("output_id"),
                    "device_id": o.get("device_id"),
                    "raw_state": o.get("state"),
                }
        return {}

    async def async_turn_on(self, **kwargs) -> None:
        raise HomeAssistantError("Outlet control not implemented yet (read-only).")

    async def async_turn_off(self, **kwargs) -> None:
        raise HomeAssistantError("Outlet control not implemented yet (read-only).")
