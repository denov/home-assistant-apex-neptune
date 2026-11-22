# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature

from .entity import ApexConfigEntry, ApexRuntimeData, async_add_discovered
from .variable import ApexLevelOutput, can_control, discover_level_outputs


async def async_setup_entry(hass, entry: ApexConfigEntry, async_add_entities):
    data = entry.runtime_data
    if not can_control(entry, data.settings.fan_outputs):
        return
    async_add_discovered(
        entry, async_add_entities, discover_level_outputs(data, data.settings.fan_outputs, ApexLevelFan)
    )


class ApexLevelFan(ApexLevelOutput, FanEntity):
    """A pump on a variable (0-10V) output. Its speed sets the program to "Set <percent>"."""

    _attr_supported_features = FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    _attr_speed_count = 100
    _attr_icon = "mdi:pump"
    # Home Assistant 2024.8-2025.1: the TURN_ON/TURN_OFF features above are accurate
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, data: ApexRuntimeData, key: str, name: str):
        super().__init__(data, key, f"output_{key}_fan", name)

    @property
    def is_on(self) -> bool | None:
        return self._is_on()

    @property
    def percentage(self) -> int | None:
        is_on = self._is_on()
        if is_on is False:
            return 0
        level = self._level()
        return round(level) if level is not None else None

    async def async_set_percentage(self, percentage: int) -> None:
        await self._async_set_level(percentage)

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any) -> None:
        await self._async_set_level(percentage if percentage else self._on_level())

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_level(0)
