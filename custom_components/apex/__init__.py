from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    CONF_IP_ADDRESS,
    CONF_USERNAME,
    CONF_PASSWORD,
    DEFAULT_USERNAME,
    DEFAULT_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    CONF_TIMEOUT,
    DEFAULT_TIMEOUT,
)
from .coordinator import ApexDataUpdateCoordinator
from .const import CONF_LINE_VOLTAGE, DEFAULT_LINE_VOLTAGE, CONF_ENABLE_PER_OUTLET_POWER, \
    DEFAULT_ENABLE_PER_OUTLET_POWER

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS: list[str] = ["sensor", "switch", "binary_sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    merged = {**entry.data, **entry.options}

    ip = merged[CONF_IP_ADDRESS]
    username = merged.get(CONF_USERNAME, DEFAULT_USERNAME)
    password = merged.get(CONF_PASSWORD, DEFAULT_PASSWORD)

    line_voltage = int(merged.get(CONF_LINE_VOLTAGE, DEFAULT_LINE_VOLTAGE))
    enable_per_outlet_power = bool(merged.get(CONF_ENABLE_PER_OUTLET_POWER, DEFAULT_ENABLE_PER_OUTLET_POWER))

    scan_interval = int(merged.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    timeout_seconds = int(merged.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))

    url = f"http://{ip}/cgi-bin/status.xml"

    coordinator = ApexDataUpdateCoordinator(
        hass,
        url=url,
        username=username,
        password=password,
        name=f"Apex @ {ip}",
        update_interval=timedelta(seconds=scan_interval),
        timeout_seconds=timeout_seconds,
    )
    coordinator.line_voltage = line_voltage

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "ip": ip,
        "line_voltage": line_voltage,
        "enable_per_outlet_power": enable_per_outlet_power,
    }

    entry.async_on_unload(entry.add_update_listener(_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
