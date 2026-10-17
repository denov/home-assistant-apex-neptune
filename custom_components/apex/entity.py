# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import aiohttp
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ApexDataUpdateCoordinator
from .parsing import safe_uid_base
from .rest import ApexAuthError, ApexRestError


def device_name(meta: dict[str, Any]) -> str:
    """"Apex", or "Apex <hostname>" when the Apex was given its own hostname."""
    hostname = (meta.get("hostname") or "").strip()
    if not hostname or hostname.lower() == "apex":
        return "Apex"
    return f"Apex {hostname}"


def build_device(coordinator: ApexDataUpdateCoordinator, host: str) -> tuple[str, DeviceInfo]:
    """unique_id prefix and device for every entity of this Apex."""
    meta = (coordinator.data or {}).get("meta", {}) or {}
    serial = (meta.get("serial") or "").strip()
    # keep unique_ids stable: the serial, or the host when the Apex doesn't report one
    uid_base = safe_uid_base(serial) or safe_uid_base(host)
    device = DeviceInfo(
        identifiers={(DOMAIN, uid_base)},
        name=device_name(meta),
        manufacturer="Neptune Systems",
        model="Apex",
        serial_number=(serial or None),
        configuration_url=f"http://{host}",
        sw_version=(meta.get("software") or None),
        hw_version=(meta.get("hardware") or None),
    )
    return uid_base, device


class ApexEntity(CoordinatorEntity[ApexDataUpdateCoordinator]):
    """Named after the Apex device, so renaming the device renames its entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ApexDataUpdateCoordinator, device_info: DeviceInfo, unique_id: str, name: str):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = unique_id
        self._attr_name = name

    @property
    def available(self) -> bool:
        # don't flip everything "unavailable" due to one failed refresh
        return self.coordinator.data is not None


def find_output(data: dict[str, Any] | None, key: str) -> dict[str, Any]:
    """The output whose output_id (or name, when it has none) is key."""
    for o in (data or {}).get("outlets", []) or []:
        if ((o.get("output_id") or "").strip() or (o.get("name") or "").strip()) == key:
            return o
    return {}


def find_output_config(data: dict[str, Any] | None, did: str | None) -> dict[str, Any]:
    """The output's entry in /rest/config oconf, if the config has been read."""
    config = (data or {}).get("config") or {}
    for o in config.get("oconf") or []:
        if isinstance(o, dict) and did and o.get("did") == did:
            return o
    return {}


async def async_apex_command(coordinator: ApexDataUpdateCoordinator, action: str, command: Awaitable[Any]) -> None:
    """Run a REST command, turning failures into errors for the user, then refresh.

    action completes "Couldn't ... on the Apex", e.g. "start feed A".
    """
    try:
        await command
    except ApexAuthError as err:
        raise HomeAssistantError(f"{err}. Update the password in the Apex integration's options.") from err
    except ValueError as err:
        raise ServiceValidationError(f"Couldn't {action}: {err}") from err
    except (ApexRestError, aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise HomeAssistantError(f"Couldn't {action} on the Apex: {err}") from err

    # show the new state now: async_request_refresh is debounced and can lag ~10s
    await coordinator.async_refresh()
