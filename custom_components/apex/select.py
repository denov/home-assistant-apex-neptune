# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.apex.const import DOMAIN
from custom_components.apex.coordinator import ApexDataUpdateCoordinator
from custom_components.apex.entity import ApexEntity, async_apex_command, find_output
from custom_components.apex.rest import DOS_MAX_RATE, OUTPUT_MODES, ApexRestClient
from custom_components.apex.switch import output_labels

# select options (translated in en.json), by the mode the Apex is sent
MODE_OPTIONS = {"AUTO": "auto", "ON": "on", "OFF": "off"}

SERVICE_SET_OUTPUT = "set_output"
SERVICE_SET_VARIABLE = "set_variable"
SERVICE_SET_DOS_RATE = "set_dos_rate"

DOS_TYPES = ("dos", "dqd")


async def async_setup_entry(hass, entry, async_add_entities):
    ctx = hass.data[DOMAIN][entry.entry_id]
    rest: ApexRestClient | None = ctx.get("control")
    if rest is None:
        # changing outputs needs a password and not read-only
        return

    coordinator: ApexDataUpdateCoordinator = ctx["coordinator"]
    uid_base: str = ctx["uid_base"]
    device: DeviceInfo = ctx["device"]

    # services act on these entities, so with several Apexes there's no doubt which one is meant
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_OUTPUT,
        {vol.Required("setting"): vol.All(cv.string, vol.Upper, vol.In(OUTPUT_MODES))},
        "async_set_output",
    )
    platform.async_register_entity_service(
        SERVICE_SET_VARIABLE,
        {vol.Required("code"): vol.All(cv.string, vol.Length(min=1, max=2000))},
        "async_set_variable",
    )
    platform.async_register_entity_service(
        SERVICE_SET_DOS_RATE,
        {
            vol.Required("profile_id"): vol.All(vol.Coerce(int), vol.Range(min=1, max=32)),
            vol.Required("rate"): vol.All(vol.Coerce(float), vol.Range(min=0, max=DOS_MAX_RATE)),
        },
        "async_set_dos_rate",
    )

    added: set[str] = set()

    def _add_new():
        outlets_now = (coordinator.data or {}).get("outlets", []) or []
        lbl = output_labels(outlets_now)
        new_entities: list[ApexOutputModeSelect] = []
        for o in outlets_now:
            key = (o.get("output_id") or "").strip() or (o.get("name") or "").strip()
            if not key or not o.get("device_id"):
                continue
            unique_id = f"{uid_base}_outlet_{key}_mode".lower()
            if unique_id in added:
                continue
            new_entities.append(ApexOutputModeSelect(coordinator, device, unique_id, lbl.get(key) or key, key, rest))
            added.add(unique_id)
        if new_entities:
            async_add_entities(new_entities)

    _add_new()

    remove = coordinator.async_add_listener(_add_new)
    entry.async_on_unload(remove)


class ApexOutputModeSelect(ApexEntity, SelectEntity):
    """An output's mode: Auto (the Apex program runs it), or manually On or Off."""

    _attr_options = list(MODE_OPTIONS.values())
    _attr_translation_key = "output_mode"
    _attr_icon = "mdi:tune-variant"

    def __init__(
        self,
        coordinator: ApexDataUpdateCoordinator,
        device_info: DeviceInfo,
        unique_id: str,
        output_name: str,
        key: str,
        rest: ApexRestClient,
    ):
        super().__init__(coordinator, device_info, unique_id, f"{output_name} Mode")
        self._key = key
        self._rest = rest

    def _output(self) -> dict[str, Any]:
        return find_output(self.coordinator.data, self._key)

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None and bool(self._output())

    @property
    def current_option(self) -> str | None:
        state = (self._output().get("state") or "").strip().upper()
        if not state:
            return None
        if state in ("ON", "OFF"):
            return MODE_OPTIONS[state]
        # AON/AOF, and anything else the program sets (TBL for a pump schedule, PF1 for a profile, ...)
        return MODE_OPTIONS["AUTO"]

    @property
    def extra_state_attributes(self):
        o = self._output()
        return {"device_id": o.get("device_id"), "raw_state": o.get("state"), "apex_type": o.get("type")}

    def _did(self) -> str:
        return self._output()["device_id"]

    async def async_select_option(self, option: str) -> None:
        mode = next(m for m, opt in MODE_OPTIONS.items() if opt == option)
        await self.async_set_output(mode)

    async def async_set_output(self, setting: str) -> None:
        await async_apex_command(
            self.coordinator, f"set {self.name} to {setting}", self._rest.async_set_output(self._did(), setting)
        )

    async def async_set_variable(self, code: str) -> None:
        self.coordinator.request_extras()
        await async_apex_command(
            self.coordinator, f"set the program of {self.name}", self._rest.async_set_program(self._did(), code)
        )

    async def async_set_dos_rate(self, profile_id: int, rate: float) -> None:
        if (self._output().get("type") or "").lower() not in DOS_TYPES:
            raise ServiceValidationError(f"{self.name} isn't a DOS or DQD pump")
        self.coordinator.request_extras()
        await async_apex_command(
            self.coordinator,
            f"set the dosing rate of {self.name}",
            self._rest.async_set_dos_rate(self._did(), profile_id, rate),
        )
