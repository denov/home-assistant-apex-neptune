# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import time
from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.exceptions import HomeAssistantError

from .entity import ApexConfigEntry, ApexEntity, ApexRuntimeData, async_apex_command, no_control_reason

# The Apex stops answering while it installs; its own UI just waits. Give up after this long.
INSTALL_TIMEOUT_SECONDS = 600
INSTALL_POLL_SECONDS = 15


def aos_version(software: str | None) -> str | None:
    """AOS version as the Apex UI shows it: status reports "5.11L_6A23" where the config says "5.11_6A23"."""
    if not software:
        return None
    return software.replace("L", "").strip() or None


async def async_setup_entry(hass, entry: ApexConfigEntry, async_add_entities):
    data = entry.runtime_data
    if data.rest is None:
        # the latest version is in the config, which needs the password
        return
    async_add_entities([ApexFirmwareUpdate(data)])


class ApexFirmwareUpdate(ApexEntity, UpdateEntity):
    """AOS firmware. Installing has the Apex download the update itself."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE

    def __init__(self, data: ApexRuntimeData):
        super().__init__(data, "firmware", "Firmware")
        self._rest = data.control
        self._read_only = data.settings.read_only
        if self._rest is not None:
            # read-only installs can see an update but not start it
            self._attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def _nconf(self) -> dict[str, Any]:
        config = (self.coordinator.data or {}).get("config") or {}
        nconf = config.get("nconf")
        return nconf if isinstance(nconf, dict) else {}

    @property
    def installed_version(self) -> str | None:
        meta = (self.coordinator.data or {}).get("meta") or {}
        return aos_version(meta.get("software"))

    @property
    def latest_version(self) -> str | None:
        # until the config has been read, don't claim an update
        return aos_version(self._nconf().get("latestFirmware")) or self.installed_version

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        if self._rest is None:
            raise HomeAssistantError(no_control_reason(self._read_only))
        before = self.installed_version
        await async_apex_command(self.coordinator, "start the firmware update", self._rest.async_update_firmware())

        self._attr_in_progress = True
        self.async_write_ha_state()
        try:
            deadline = time.monotonic() + INSTALL_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(INSTALL_POLL_SECONDS)
                self.coordinator.request_extras()
                await self.coordinator.async_refresh()
                if self.coordinator.last_update_success and self.installed_version != before:
                    return
            raise HomeAssistantError(
                f"The Apex is still on {before} after {INSTALL_TIMEOUT_SECONDS // 60} minutes; check it in its web interface"
            )
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()
