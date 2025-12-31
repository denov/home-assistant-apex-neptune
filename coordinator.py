# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
import async_timeout
import xml.etree.ElementTree as ET

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)


def _to_number(s: str | None) -> float | None:
    if s is None:
        return None
    t = s.strip()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


class ApexDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches and parses Apex status.xml into a shared data dict for entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        url: str,
        username: str = "admin",
        password: str = "",
        name: str = "Apex",
        update_interval: timedelta | None = None,
        timeout_seconds: int = 10,
    ) -> None:
        self.hass = hass
        self.url = url
        self.username = username or ""
        self.password = password or ""
        self.timeout_seconds = timeout_seconds

        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=update_interval or timedelta(seconds=30),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)

        auth: aiohttp.BasicAuth | None = None
        if self.password:
            auth = aiohttp.BasicAuth(self.username or "admin", self.password)

        try:
            async with async_timeout.timeout(self.timeout_seconds):
                async with session.get(self.url, auth=auth) as resp:
                    resp.raise_for_status()
                    body = await resp.text()

            return _parse_status_xml(body)

        except (asyncio.TimeoutError, aiohttp.ClientError, ET.ParseError) as err:
            raise UpdateFailed(f"Error fetching/parsing Apex status.xml: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error updating Apex data: {err}") from err


def _parse_status_xml(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)

    meta: dict[str, Any] = {
        "software": (root.attrib.get("software") or "").strip() or None,
        "hardware": (root.attrib.get("hardware") or "").strip() or None,
        "hostname": (root.findtext("./hostname") or "").strip() or None,
        "serial": (root.findtext("./serial") or "").strip() or None,
        "timezone": (root.findtext("./timezone") or "").strip() or None,
        "date": (root.findtext("./date") or "").strip() or None,
    }

    power: dict[str, Any] = {
        "failed": (root.findtext("./power/failed") or "").strip() or None,
        "restored": (root.findtext("./power/restored") or "").strip() or None,
    }

    probes: dict[str, dict[str, Any]] = {}
    for p in root.findall("./probes/probe"):
        name = (p.findtext("name") or "").strip()
        if not name:
            continue

        value_raw = p.findtext("value")
        value = _to_number(value_raw)

        probes[name] = {
            "name": name,
            "value": value,
            "value_raw": (value_raw.strip() if value_raw else None),
            "type": (p.findtext("type") or "").strip() or None,
        }

    outlets: list[dict[str, Any]] = []
    for o in root.findall("./outlets/outlet"):
        name = (o.findtext("name") or "").strip()
        if not name:
            continue
        outlets.append(
            {
                "name": name,
                "output_id": (o.findtext("outputID") or "").strip() or None,
                "state": (o.findtext("state") or "").strip() or None,
                "device_id": (o.findtext("deviceID") or "").strip() or None,
            }
        )

    return {
        "meta": meta,
        "power": power,
        "probes": probes,
        "outlets": outlets,
    }

# Allow config_flow to reuse parsing for "Test Connection"
__all__ = ["ApexDataUpdateCoordinator", "_parse_status_xml"]
