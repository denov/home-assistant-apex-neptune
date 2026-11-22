# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTime
from homeassistant.helpers.entity import EntityCategory

from .entity import ApexConfigEntry, ApexEntity, ApexRuntimeData, async_add_discovered, async_apex_command
from .rest import FEED_LETTERS, FEED_MAX_SECONDS, FEED_MIN_SECONDS


def feed_durations(data: dict[str, Any] | None) -> list[Any]:
    """Seconds each feed cycle (A-D) lasts, from the REST config (misc feedInterval)."""
    misc = ((data or {}).get("config") or {}).get("misc")
    intervals = misc.get("feedInterval") if isinstance(misc, dict) else None
    return intervals if isinstance(intervals, list) else []


async def async_setup_entry(hass, entry: ApexConfigEntry, async_add_entities):
    data = entry.runtime_data
    if data.control is None:
        # changing the Apex needs a password and not read-only
        return

    def discover(d: dict[str, Any]):
        for feed in FEED_LETTERS:
            if len(feed_durations(d)) >= feed:
                yield f"feed_{feed}", lambda feed=feed: ApexFeedDuration(data, feed)

    async_add_discovered(entry, async_add_entities, discover)


class ApexFeedDuration(ApexEntity, NumberEntity):
    """How long a feed cycle (A-D) lasts, as set on the Apex's Misc setup page."""

    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = FEED_MIN_SECONDS
    _attr_native_max_value = FEED_MAX_SECONDS
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer-cog-outline"

    def __init__(self, data: ApexRuntimeData, feed: int):
        letter = FEED_LETTERS[feed]
        super().__init__(data, f"feed_{letter}_duration", f"Feed {letter} Duration")
        self._rest = data.control
        self._feed = feed

    def _seconds(self) -> Any:
        intervals = feed_durations(self.coordinator.data)
        return intervals[self._feed - 1] if len(intervals) >= self._feed else None

    @property
    def available(self) -> bool:
        return super().available and self._seconds() is not None

    @property
    def native_value(self) -> float | None:
        seconds = self._seconds()
        return seconds if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else None

    async def async_set_native_value(self, value: float) -> None:
        letter = FEED_LETTERS[self._feed]
        # the duration is in the config, which is otherwise read every few minutes
        self.coordinator.request_extras()
        await async_apex_command(
            self.coordinator,
            f"set how long feed {letter} lasts",
            self._rest.async_set_feed_duration(self._feed, round(value)),
        )
