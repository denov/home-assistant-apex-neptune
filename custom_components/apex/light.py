# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity

from .entity import ApexConfigEntry, ApexRuntimeData, async_add_discovered
from .variable import ApexLevelOutput, can_control, discover_level_outputs


async def async_setup_entry(hass, entry: ApexConfigEntry, async_add_entities):
    data = entry.runtime_data
    if not can_control(entry, data.settings.light_outputs):
        return
    async_add_discovered(
        entry, async_add_entities, discover_level_outputs(data, data.settings.light_outputs, ApexLevelLight)
    )


def brightness_to_percent(brightness: int) -> int:
    return max(1, min(100, round(brightness * 100 / 255)))


class ApexLevelLight(ApexLevelOutput, LightEntity):
    """A light on a variable (0-10V) output. Brightness sets its program to "Set <percent>"."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, data: ApexRuntimeData, key: str, name: str):
        super().__init__(data, key, f"output_{key}_light", name)

    @property
    def is_on(self) -> bool | None:
        return self._is_on()

    @property
    def brightness(self) -> int | None:
        level = self._level()
        if level is None or not self._is_on():
            return None
        return max(1, min(255, round(level * 255 / 100)))

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        await self._async_set_level(brightness_to_percent(brightness) if brightness is not None else self._on_level())

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_level(0)
