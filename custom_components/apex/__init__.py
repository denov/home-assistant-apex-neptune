from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, ApexSettings
from .coordinator import ApexDataUpdateCoordinator
from .entity import ApexConfigEntry, ApexRuntimeData, build_device
from .rest import ApexRestClient

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS: list[str] = ["sensor", "switch", "binary_sensor", "button", "select", "update"]


async def async_setup_entry(hass: HomeAssistant, entry: ApexConfigEntry) -> bool:
    settings = ApexSettings.from_dict({**entry.data, **entry.options})

    # the REST API needs a login; reading status.json works without a password
    rest: ApexRestClient | None = None
    if settings.password:
        rest = ApexRestClient(
            async_get_clientsession(hass),
            settings.host_url,
            settings.username,
            settings.password,
            timeout_seconds=settings.timeout,
        )

    coordinator = ApexDataUpdateCoordinator(hass, entry, settings, rest)
    await coordinator.async_config_entry_first_refresh()

    uid_base, device = build_device(coordinator, settings.host)
    # entries used to be keyed on the host; the serial survives a new IP address
    if entry.unique_id != uid_base and not any(
        e.unique_id == uid_base for e in hass.config_entries.async_entries(DOMAIN)
    ):
        hass.config_entries.async_update_entry(entry, unique_id=uid_base)

    entry.runtime_data = ApexRuntimeData(settings, coordinator, rest, uid_base, device)

    entry.async_on_unload(entry.add_update_listener(_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ApexConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _update_listener(hass: HomeAssistant, entry: ApexConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
