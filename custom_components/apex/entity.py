# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ApexSettings
from .coordinator import ApexDataUpdateCoordinator
from .parsing import apex_uid
from .rest import ApexAuthError, ApexRestClient, ApexRestError

DOS_TYPES = ("dos", "dqd")
# output states that are on (A = under Apex program control)
ON_STATES = ("ON", "AON")


@dataclass
class ApexRuntimeData:
    """What a loaded config entry shares with its platforms (entry.runtime_data)."""

    settings: ApexSettings
    coordinator: ApexDataUpdateCoordinator
    # with a password
    rest: ApexRestClient | None
    uid_base: str
    device: DeviceInfo
    # the modules last registered as devices (see async_sync_module_devices)
    module_devices: list[tuple[Any, ...]] = field(default_factory=list)

    @property
    def control(self) -> ApexRestClient | None:
        """The REST client for changing the Apex: needs a login and isn't read-only."""
        return None if self.settings.read_only else self.rest


type ApexConfigEntry = ConfigEntry[ApexRuntimeData]


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
    uid_base = apex_uid(meta, host)
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


@callback
def async_add_discovered(
    entry: ApexConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discover: Callable[[dict[str, Any]], Iterable[tuple[str, Callable[[], Entity]]]],
) -> None:
    """Add what discover(data) finds, now and whenever a poll turns up something new.

    discover yields (key, factory) pairs; each key's entity is made only once.
    """
    coordinator = entry.runtime_data.coordinator
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        new: list[Entity] = []
        for key, make in discover(coordinator.data or {}):
            if key not in added:
                added.add(key)
                new.append(make())
        if new:
            # coordinator data is already fresh; update_before_add would poll the Apex again
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


# AquaBus module models by hwtype, as the Apex names them
MODULE_MODELS: dict[str, str] = {
    "1LNK": "1Link",
    "AFS": "Automatic Feeding System",
    "ALD": "Advanced Leak Detection",
    "ASM": "Advanced Sensors Module",
    "AWM": "AI Wireless Module",
    "COR": "COR",
    "Disp": "Display",
    "DOS": "DOS",
    "DQD": "DOS QD",
    "EB4": "Energy Bar 4",
    "EB6": "Energy Bar 6",
    "EB632": "Energy Bar 632",
    "EB8": "Energy Bar 8",
    "EB832": "Energy Bar 832",
    "FMM": "Fluid Monitoring Module",
    "IOTA": "IOTA Module",
    "LDM": "Leak Detection Module",
    "LSM": "Lunar Simulation Module",
    "MXM": "Mobius Expansion Module",
    "PM1": "Probe Module 1",
    "PM2": "Probe Module 2",
    "PM3": "Probe Module 3",
    "TH": "Trident TH",
    "TNP": "Trident NP",
    "TRI": "Trident",
    "VDM": "Variable Dimming Module",
    "WXM": "Wireless Expansion Module",
}


def module_config(data: dict[str, Any] | None, abaddr: Any) -> dict[str, Any]:
    """A module's entry in /rest/config mconf, by its AquaBus address."""
    config = (data or {}).get("config") or {}
    for m in config.get("mconf") or []:
        if isinstance(m, dict) and m.get("abaddr") == abaddr:
            return m
    return {}


def find_module(data: dict[str, Any] | None, abaddr: Any) -> dict[str, Any]:
    """A module's entry in /rest/status modules, by its AquaBus address."""
    for m in (data or {}).get("modules", []) or []:
        if m.get("abaddr") == abaddr:
            return m
    return {}


def module_name(data: dict[str, Any] | None, module: dict[str, Any]) -> str:
    """What the module is called on the Apex (e.g. EB8_DOWN2), else its type and address."""
    name = module_config(data, module.get("abaddr")).get("name") or module.get("name")
    return str(name) if name else f"{module.get('hwtype')}_{module.get('abaddr')}"


def module_device(data: ApexRuntimeData, abaddr: Any) -> DeviceInfo:
    """The device of an AquaBus module, registered by async_sync_module_devices."""
    return DeviceInfo(identifiers={(DOMAIN, f"{data.uid_base}_module_{abaddr}")})


@callback
def async_sync_module_devices(hass: HomeAssistant, entry: ApexConfigEntry) -> None:
    """Register a device per AquaBus module, connected through the Apex, and keep it current.

    Done here rather than through each entity's DeviceInfo, since DeviceInfo's via_device is
    deprecated in newer Home Assistant and its replacement doesn't exist in older ones.
    """
    data = entry.runtime_data
    d = data.coordinator.data or {}
    modules = [m for m in d.get("modules", []) or [] if isinstance(m.get("abaddr"), int)]
    seen = [(m["abaddr"], module_name(d, m), m.get("hwtype"), m.get("swrev"), m.get("hwrev")) for m in modules]
    if not modules or seen == data.module_devices:
        return
    data.module_devices = seen

    registry = dr.async_get(hass)
    apex = registry.async_get_or_create(config_entry_id=entry.entry_id, **data.device)
    for abaddr, name, hwtype, swrev, hwrev in seen:
        hwtype = str(hwtype or "")
        device = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers=module_device(data, abaddr)["identifiers"],
            name=name,
            manufacturer="Neptune Systems",
            model=MODULE_MODELS.get(hwtype, hwtype or None),
            sw_version=str(swrev) if swrev is not None else None,
            hw_version=str(hwrev) if hwrev is not None else None,
        )
        if device.via_device_id != apex.id:
            registry.async_update_device(device.id, via_device_id=apex.id)


class ApexEntity(CoordinatorEntity[ApexDataUpdateCoordinator]):
    """Named after its device (the Apex, or one of its modules), so renaming the device renames its entities."""

    _attr_has_entity_name = True

    def __init__(self, data: ApexRuntimeData, key: str, name: str, device: DeviceInfo | None = None):
        super().__init__(data.coordinator)
        self._attr_device_info = device or data.device
        self._attr_unique_id = f"{data.uid_base}_{key}".lower()
        self._attr_name = name

    @property
    def available(self) -> bool:
        # one failed poll doesn't make everything unavailable; a few in a row do
        return self.coordinator.data is not None and not self.coordinator.stale


def output_key(o: dict[str, Any]) -> str:
    """An output's key in unique_ids: its output ID, or its name when it has none."""
    return (o.get("output_id") or "").strip() or (o.get("name") or "").strip()


def output_labels(outlets: list[dict[str, Any]]) -> dict[str, str]:
    """Label per output key: the Apex name, plus the output ID when two outputs share a name."""
    counts: dict[str, int] = {}
    for o in outlets:
        nm = (o.get("name") or "").strip()
        counts[nm] = counts.get(nm, 0) + 1

    out: dict[str, str] = {}
    for o in outlets:
        nm = (o.get("name") or "").strip()
        oid = (o.get("output_id") or "").strip()
        out[output_key(o)] = f"{nm} ({oid})" if counts[nm] > 1 and oid else nm
    return out


def find_output(data: dict[str, Any] | None, key: str) -> dict[str, Any]:
    """The output whose output_key is key."""
    for o in (data or {}).get("outlets", []) or []:
        if output_key(o) == key:
            return o
    return {}


def find_output_config(data: dict[str, Any] | None, did: str | None) -> dict[str, Any]:
    """The output's entry in /rest/config oconf, if the config has been read."""
    config = (data or {}).get("config") or {}
    for o in config.get("oconf") or []:
        if isinstance(o, dict) and did and o.get("did") == did:
            return o
    return {}


def is_on_state(state: str | None) -> bool:
    return (state or "").strip().upper() in ON_STATES


class ApexOutputEntity(ApexEntity):
    """An entity for one Apex output, found by its output key."""

    def __init__(self, data: ApexRuntimeData, key: str, uid_key: str, name: str):
        super().__init__(data, uid_key, name)
        self._key = key

    def _output(self) -> dict[str, Any]:
        return find_output(self.coordinator.data, self._key)

    def _did(self) -> str:
        did = self._output().get("device_id")
        if not did:
            raise HomeAssistantError(f"The Apex didn't report a device id for {self.name}")
        return did

    @property
    def available(self) -> bool:
        return super().available and bool(self._output())


def no_control_reason(read_only: bool) -> str:
    if read_only:
        return "The Apex integration is set to read-only. Turn that off in its options to control outputs."
    return "Controlling the Apex needs its password. Add it in the Apex integration's options."


async def async_apex_command(coordinator: ApexDataUpdateCoordinator, action: str, command: Awaitable[Any]) -> None:
    """Run a REST command, turning failures into errors for the user, then refresh.

    action completes "Couldn't ... on the Apex", e.g. "start feed A".
    """
    try:
        await command
    except ApexAuthError as err:
        coordinator.async_start_reauth()
        raise HomeAssistantError(
            f"{err}. Enter the Apex password again under Settings > Devices & services."
        ) from err
    except ValueError as err:
        raise ServiceValidationError(f"Couldn't {action}: {err}") from err
    except (ApexRestError, aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise HomeAssistantError(f"Couldn't {action} on the Apex: {err}") from err

    # show the new state now: async_request_refresh is debounced and can lag ~10s
    await coordinator.async_refresh()
