# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.apex.const import DOMAIN
from custom_components.apex.coordinator import ApexDataUpdateCoordinator
from custom_components.apex.entity import ApexEntity, async_apex_command
from custom_components.apex.rest import FEED_LETTERS, ApexRestClient


async def async_setup_entry(hass, entry, async_add_entities):
    ctx = hass.data[DOMAIN][entry.entry_id]
    rest: ApexRestClient | None = ctx.get("control")
    if rest is None:
        # feed control needs a password (set it in the integration's options) and not read-only
        return

    coordinator: ApexDataUpdateCoordinator = ctx["coordinator"]
    uid_base: str = ctx["uid_base"]
    device: DeviceInfo = ctx["device"]

    entities: list[ApexFeedButton] = [
        ApexFeedButton(coordinator, rest, device, uid_base, feed) for feed in FEED_LETTERS
    ]
    entities.append(ApexFeedButton(coordinator, rest, device, uid_base, None))
    async_add_entities(entities)


class ApexFeedButton(ApexEntity, ButtonEntity):
    """Start feed cycle A-D, or cancel the running one (feed=None)."""

    def __init__(
        self,
        coordinator: ApexDataUpdateCoordinator,
        rest: ApexRestClient,
        device_info: DeviceInfo,
        uid_base: str,
        feed: int | None,
    ):
        if feed is None:
            super().__init__(coordinator, device_info, f"{uid_base}_feed_cancel", "Cancel Feed")
            self._attr_icon = "mdi:fish-off"
        else:
            letter = FEED_LETTERS[feed]
            super().__init__(coordinator, device_info, f"{uid_base}_feed_{letter}".lower(), f"Feed {letter}")
            self._attr_icon = "mdi:fish"
        self._rest = rest
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
