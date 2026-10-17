# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio

import aiohttp
from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.apex.const import DOMAIN
from custom_components.apex.coordinator import ApexDataUpdateCoordinator
from custom_components.apex.parsing import one_apex_prefix, safe_uid_base
from custom_components.apex.rest import FEED_LETTERS, ApexAuthError, ApexRestClient, ApexRestError


async def async_setup_entry(hass, entry, async_add_entities):
    ctx = hass.data[DOMAIN][entry.entry_id]
    rest: ApexRestClient | None = ctx.get("rest")
    if rest is None:
        # feed control needs a password (set it in the integration's options)
        return

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

    entities: list[ApexFeedButton] = [
        ApexFeedButton(coordinator, rest, device, uid_base, feed) for feed in FEED_LETTERS
    ]
    entities.append(ApexFeedButton(coordinator, rest, device, uid_base, None))
    async_add_entities(entities)


class ApexFeedButton(CoordinatorEntity[ApexDataUpdateCoordinator], ButtonEntity):
    """Start feed cycle A-D, or cancel the running one (feed=None)."""

    def __init__(
        self,
        coordinator: ApexDataUpdateCoordinator,
        rest: ApexRestClient,
        device_info: DeviceInfo,
        uid_base: str,
        feed: int | None,
    ):
        super().__init__(coordinator)
        self._rest = rest
        self._feed = feed
        self._attr_device_info = device_info

        if feed is None:
            self._attr_name = one_apex_prefix("Cancel Feed")
            self._attr_unique_id = f"{uid_base}_feed_cancel"
            self._attr_icon = "mdi:fish-off"
        else:
            letter = FEED_LETTERS[feed]
            self._attr_name = one_apex_prefix(f"Feed {letter}")
            self._attr_unique_id = f"{uid_base}_feed_{letter}".lower()
            self._attr_icon = "mdi:fish"

    @property
    def available(self) -> bool:
        # control doesn't depend on the last poll succeeding
        return True

    async def async_press(self) -> None:
        try:
            if self._feed is None:
                await self._rest.async_cancel_feed()
            else:
                await self._rest.async_start_feed(self._feed)
        except ApexAuthError as err:
            raise HomeAssistantError(
                f"{err}. Update the password in the Apex integration's options."
            ) from err
        except (ApexRestError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            action = "cancel the feed" if self._feed is None else f"start feed {FEED_LETTERS[self._feed]}"
            raise HomeAssistantError(f"Couldn't {action} on the Apex: {err}") from err

        # show the new feed state now: async_request_refresh is debounced and can lag ~10s
        await self.coordinator.async_refresh()
