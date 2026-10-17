# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from homeassistant.components.button import ButtonEntity

from .entity import ApexConfigEntry, ApexEntity, ApexRuntimeData, async_apex_command
from .rest import FEED_LETTERS


async def async_setup_entry(hass, entry: ApexConfigEntry, async_add_entities):
    data = entry.runtime_data
    if data.control is None:
        # feed control needs a password (set it in the integration's options) and not read-only
        return
    async_add_entities([*(ApexFeedButton(data, feed) for feed in FEED_LETTERS), ApexFeedButton(data, None)])


class ApexFeedButton(ApexEntity, ButtonEntity):
    """Start feed cycle A-D, or cancel the running one (feed=None)."""

    def __init__(self, data: ApexRuntimeData, feed: int | None):
        if feed is None:
            super().__init__(data, "feed_cancel", "Cancel Feed")
            self._attr_icon = "mdi:fish-off"
        else:
            letter = FEED_LETTERS[feed]
            super().__init__(data, f"feed_{letter}", f"Feed {letter}")
            self._attr_icon = "mdi:fish"
        self._rest = data.control
        self._feed = feed

    @property
    def available(self) -> bool:
        # control doesn't depend on the last poll succeeding
        return True

    async def async_press(self) -> None:
        if self._feed is None:
            await async_apex_command(self.coordinator, "cancel the feed", self._rest.async_cancel_feed())
        else:
            await async_apex_command(
                self.coordinator, f"start feed {FEED_LETTERS[self._feed]}", self._rest.async_start_feed(self._feed)
            )
