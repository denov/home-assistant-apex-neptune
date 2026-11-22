# SPDX-License-Identifier: AGPL-3.0-or-later
"""Variable (0-10V) outputs controlled as lights or fans: picked one by one in the options.

The level is set the way the apex.set_variable action does: by replacing the output's
program with "Set <percent>", so whatever program it had is gone.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .entity import (
    ApexConfigEntry,
    ApexOutputEntity,
    ApexRuntimeData,
    async_apex_command,
    find_output_config,
    output_key,
    output_labels,
)
from .parsing import program_setting

# output types that run at a level set by "Set <percent>"
LEVEL_TYPES = ("variable",)

# output states that are off, and a manual mode that overrides the program
OFF_STATES = ("OFF", "AOF")
MANUAL_STATES = ("ON", "OFF")


def is_level_output(o: dict[str, Any]) -> bool:
    return (o.get("type") or "").lower() in LEVEL_TYPES and bool(o.get("device_id"))


def level_outputs(data: dict[str, Any] | None) -> dict[str, str]:
    """Label per output key of the outputs that can be controlled as a light or fan."""
    outlets = (data or {}).get("outlets", []) or []
    labels = output_labels(outlets)
    return {output_key(o): labels.get(output_key(o)) or output_key(o) for o in outlets if is_level_output(o)}


def discover_level_outputs(
    data: ApexRuntimeData, keys: Iterable[str], make: Callable[[ApexRuntimeData, str, str], Entity]
) -> Callable[[dict[str, Any]], Iterable[tuple[str, Callable[[], Entity]]]]:
    """A discover function (for async_add_discovered) for the outputs picked in the options."""
    picked = set(keys)

    def discover(d: dict[str, Any]):
        for key, label in level_outputs(d).items():
            if key in picked:
                yield key, lambda key=key, label=label: make(data, key, label)

    return discover


def can_control(entry: ApexConfigEntry, keys: Iterable[str]) -> bool:
    return entry.runtime_data.control is not None and bool(tuple(keys))


class ApexLevelOutput(ApexOutputEntity):
    """An output run at a level (%) by its program."""

    def __init__(self, data: ApexRuntimeData, key: str, uid_key: str, name: str):
        super().__init__(data, key, uid_key, name)
        self._rest = data.control
        # turning on without a level goes back to this
        self._last_level: float | None = None

    def _state(self) -> str:
        return (self._output().get("state") or "").strip().upper()

    def _level(self) -> float | None:
        """The level (%), as the Apex reports it, else as the program sets it ("Set 75")."""
        o = self._output()
        intensity = o.get("intensity")
        if intensity is not None:
            return float(intensity)
        setting = program_setting(find_output_config(self.coordinator.data, o.get("device_id")).get("prog"))
        if isinstance(setting, (int, float)):
            return float(setting)
        if isinstance(setting, str) and setting.upper() in ("ON", "OFF"):
            return 100.0 if setting.upper() == "ON" else 0.0
        # e.g. "Set PF1": a profile
        return None

    def _is_on(self) -> bool | None:
        if self._state() in OFF_STATES:
            return False
        level = self._level()
        if level is not None:
            return level > 0
        if self._state() in ("ON", "AON"):
            return True
        return None

    def _on_level(self) -> int:
        """The level to turn on at: the current one, else the last one seen, else 100%."""
        level = self._level()
        if self._is_on() and level:
            return round(level)
        return round(self._last_level) if self._last_level else 100

    @callback
    def _handle_coordinator_update(self) -> None:
        level = self._level()
        if level and self._is_on():
            self._last_level = level
        super()._handle_coordinator_update()

    async def _async_set_level(self, percent: int) -> None:
        """Run the output at percent (0 is off) by replacing its program."""
        did = self._did()
        manual = self._state() in MANUAL_STATES
        program = f"Set {percent}" if percent > 0 else "Set OFF"
        rest = self._rest

        async def _run() -> None:
            await rest.async_set_program(did, program)
            if manual:
                # a manual ON or OFF overrides the program: hand the output back to it
                await rest.async_set_output(did, "AUTO")

        # the program is in the config, which is otherwise read every few minutes
        self.coordinator.request_extras()
        await async_apex_command(self.coordinator, f"set {self.name} to {percent}%", _run())

    @property
    def extra_state_attributes(self):
        o = self._output()
        config = find_output_config(self.coordinator.data, o.get("device_id"))
        return {"device_id": o.get("device_id"), "raw_state": o.get("state"), "program": config.get("prog")}
