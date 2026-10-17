# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform

from .entity import (
    DOS_TYPES,
    ApexConfigEntry,
    ApexOutputEntity,
    ApexRuntimeData,
    async_add_discovered,
    async_apex_command,
    output_key,
    output_labels,
)
from .rest import DOS_MAX_RATE, OUTPUT_MODES

# select options (translated in en.json), by the mode the Apex is sent
MODE_OPTIONS = {"AUTO": "auto", "ON": "on", "OFF": "off"}

SERVICE_SET_OUTPUT = "set_output"
SERVICE_SET_VARIABLE = "set_variable"
SERVICE_SET_DOS_RATE = "set_dos_rate"


async def async_setup_entry(hass, entry: ApexConfigEntry, async_add_entities):
    data = entry.runtime_data
    if data.control is None:
        # changing outputs needs a password and not read-only
        return

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

    def discover(d: dict[str, Any]):
        outlets = d.get("outlets", []) or []
        labels = output_labels(outlets)
        for o in outlets:
            key = output_key(o)
            if key and o.get("device_id"):
                yield key, lambda key=key: ApexOutputModeSelect(data, key, labels.get(key) or key)

    async_add_discovered(entry, async_add_entities, discover)


class ApexOutputModeSelect(ApexOutputEntity, SelectEntity):
    """An output's mode: Auto (the Apex program runs it), or manually On or Off."""

    _attr_options = list(MODE_OPTIONS.values())
    _attr_translation_key = "output_mode"
    _attr_icon = "mdi:tune-variant"

    def __init__(self, data: ApexRuntimeData, key: str, output_name: str):
        super().__init__(data, key, f"outlet_{key}_mode", f"{output_name} Mode")
        self._rest = data.control

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

    async def async_select_option(self, option: str) -> None:
        mode = next(m for m, opt in MODE_OPTIONS.items() if opt == option)
        await self.async_set_output(mode)

    async def async_set_output(self, setting: str) -> None:
        await async_apex_command(
            self.coordinator, f"set {self.name} to {setting}", self._rest.async_set_output(self._did(), setting)
        )

    async def async_set_variable(self, code: str) -> None:
        did = self._did()
        self.coordinator.request_extras()
        await async_apex_command(
            self.coordinator, f"set the program of {self.name}", self._rest.async_set_program(did, code)
        )

    async def async_set_dos_rate(self, profile_id: int, rate: float) -> None:
        if (self._output().get("type") or "").lower() not in DOS_TYPES:
            raise ServiceValidationError(f"{self.name} isn't a DOS or DQD pump")
        did = self._did()
        self.coordinator.request_extras()
        await async_apex_command(
            self.coordinator,
            f"set the dosing rate of {self.name}",
            self._rest.async_set_dos_rate(did, profile_id, rate),
        )
