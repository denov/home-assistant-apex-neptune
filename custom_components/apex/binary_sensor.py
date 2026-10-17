# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.apex.const import DOMAIN
from custom_components.apex.coordinator import ApexDataUpdateCoordinator
from custom_components.apex.parsing import one_apex_prefix, safe_uid_base


async def async_setup_entry(hass, entry, async_add_entities):
    ctx = hass.data[DOMAIN][entry.entry_id]
    coordinator: ApexDataUpdateCoordinator = ctx["coordinator"]
    ip: str = ctx["ip"]

    meta = (coordinator.data or {}).get("meta", {}) or {}
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

    added: set[str] = set()

    def _add_new():
        inputs_now = (coordinator.data or {}).get("inputs", {}) or {}
        new_entities: list[ApexDigitalInput] = []

        for did in sorted(inputs_now):
            unique_id = f"{uid_base}_input_{did}".lower()
            if unique_id in added:
                continue
            label = one_apex_prefix(inputs_now[did].get("name") or did)
            new_entities.append(ApexDigitalInput(coordinator, device, unique_id, label, did))
            added.add(unique_id)

        if new_entities:
            # coordinator data is already fresh; update_before_add would poll the Apex again
            async_add_entities(new_entities)

    _add_new()

    remove = coordinator.async_add_listener(_add_new)
    entry.async_on_unload(remove)


class ApexDigitalInput(CoordinatorEntity[ApexDataUpdateCoordinator], BinarySensorEntity):
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
        super().__init__(coordinator)
        self._did = did
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_device_info = device_info

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
