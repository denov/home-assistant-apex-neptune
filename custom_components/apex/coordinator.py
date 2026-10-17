# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import xml.etree.ElementTree as ET

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

# status.json reports digital inputs as 0 (OPEN) or 200 (CLOSED); unconnected inputs read OPEN.
DIGITAL_OPEN = 0
DIGITAL_CLOSED = 200

# How often to look for status.json again on an Apex that only serves status.xml.
JSON_RETRY_SECONDS = 3600


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


async def async_fetch_status(
    session: aiohttp.ClientSession,
    base_url: str,
    *,
    auth: aiohttp.BasicAuth | None = None,
    timeout_seconds: int = 10,
    try_json: bool = True,
) -> dict[str, Any]:
    """Fetch status.json, or status.xml when status.json isn't usable (data["source"] says which).

    Older firmware may answer status.json with any HTTP error or a non-JSON page, so those fall
    back to status.xml. Timeouts and connection errors mean the Apex is unreachable and are raised,
    as are errors from status.xml: asyncio.TimeoutError, aiohttp.ClientError or ET.ParseError.
    One timeout covers both requests.
    """
    async with asyncio.timeout(timeout_seconds):
        if try_json:
            async with session.get(f"{base_url}/status.json", auth=auth) as resp:
                if resp.status < 400:
                    try:
                        return _parse_status_json(await resp.text())
                    except ValueError as err:
                        _LOGGER.debug("Apex status.json is not usable (%s); trying status.xml", err)
                else:
                    _LOGGER.debug("Apex status.json returned HTTP %s; trying status.xml", resp.status)

        async with session.get(f"{base_url}/status.xml", auth=auth) as resp:
            resp.raise_for_status()
            return _parse_status_xml(await resp.text())


class ApexDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches and parses Apex status.json (or status.xml) into a shared data dict for entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        base_url: str,
        username: str = "admin",
        password: str = "",
        name: str = "Apex",
        update_interval: timedelta | None = None,
        timeout_seconds: int = 10,
    ) -> None:
        self.hass = hass
        self.base_url = base_url
        self.username = username or ""
        self.password = password or ""
        self.timeout_seconds = timeout_seconds
        # Stop asking for status.json only on an Apex where it has never worked,
        # and even then look again occasionally (e.g. after a firmware update).
        self._json_seen = False
        self._json_retry_at: float | None = None
        self._json_failing = False

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

        try_json = self._json_retry_at is None or time.monotonic() >= self._json_retry_at

        try:
            data = await async_fetch_status(
                session,
                self.base_url,
                auth=auth,
                timeout_seconds=self.timeout_seconds,
                try_json=try_json,
            )
        except (asyncio.TimeoutError, aiohttp.ClientError, ET.ParseError) as err:
            raise UpdateFailed(f"Error fetching/parsing Apex status: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error updating Apex data: {err}") from err

        if data["source"] == "json":
            if self._json_failing:
                _LOGGER.info("Apex status.json is back; digital inputs available again")
            self._json_seen = True
            self._json_failing = False
            self._json_retry_at = None
        elif try_json and self._json_seen:
            # worked before: a hiccup, not old firmware; keep asking every poll
            if not self._json_failing:
                _LOGGER.warning("Apex status.json failed; digital inputs unavailable until it recovers")
            self._json_failing = True
        elif try_json:
            if self._json_retry_at is None:
                _LOGGER.info("Apex has no usable status.json; using status.xml (digital inputs not available)")
            self._json_retry_at = time.monotonic() + JSON_RETRY_SECONDS

        return data


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
        "source": "xml",
        "meta": meta,
        "power": power,
        "probes": probes,
        "outlets": outlets,
        "inputs": {},  # status.xml doesn't report digital inputs
    }


def _format_apex_time(epoch: Any, tz_hours: float) -> str | None:
    """status.json times are UTC epochs; format them like status.xml (Apex local time)."""
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool) or epoch <= 0:
        return None
    local = datetime.fromtimestamp(epoch, timezone.utc) + timedelta(hours=tz_hours)
    return local.strftime("%m/%d/%Y %H:%M:%S")


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    return str(v).strip() or None


def _parse_status_json(json_text: str) -> dict[str, Any]:
    """Parse status.json into the same shape as _parse_status_xml, plus digital inputs."""
    doc = json.loads(json_text)
    istat = doc.get("istat") if isinstance(doc, dict) else None
    if not isinstance(istat, dict):
        raise ValueError("status.json has no istat object")

    tz = _str_or_none(istat.get("timezone"))
    tz_hours = _to_number(tz) or 0.0

    meta: dict[str, Any] = {
        "software": _str_or_none(istat.get("software")),
        "hardware": _str_or_none(istat.get("hardware")),
        "hostname": _str_or_none(istat.get("hostname")),
        "serial": _str_or_none(istat.get("serial")),
        "timezone": tz,
        "date": _format_apex_time(istat.get("date"), tz_hours),
    }

    power_raw = istat.get("power") or {}
    power: dict[str, Any] = {
        "failed": _format_apex_time(power_raw.get("failed"), tz_hours),
        "restored": _format_apex_time(power_raw.get("restored"), tz_hours),
    }

    probes: dict[str, dict[str, Any]] = {}
    inputs: dict[str, dict[str, Any]] = {}
    for i in istat.get("inputs", []) or []:
        if not isinstance(i, dict):
            continue
        typ = _str_or_none(i.get("type"))
        name = _str_or_none(i.get("name"))
        value = i.get("value")

        if (typ or "").lower() == "digital":
            did = _str_or_none(i.get("did"))
            if not did:
                continue
            if value == DIGITAL_CLOSED:
                state = "CLOSED"
            elif value == DIGITAL_OPEN:
                state = "OPEN"
            else:
                state = None
            inputs[did] = {"did": did, "name": name or did, "value": value, "state": state}
            continue

        if not name:
            continue
        value_raw = _str_or_none(value)
        probes[name] = {
            "name": name,
            "value": _to_number(value_raw),
            "value_raw": value_raw,
            "type": typ,
        }

    outlets: list[dict[str, Any]] = []
    for o in istat.get("outputs", []) or []:
        if not isinstance(o, dict):
            continue
        name = _str_or_none(o.get("name"))
        if not name:
            continue
        status = o.get("status") or []
        outlets.append(
            {
                "name": name,
                "output_id": _str_or_none(o.get("ID")),
                "state": _str_or_none(status[0]) if status else None,
                "device_id": _str_or_none(o.get("did")),
            }
        )

    return {
        "source": "json",
        "meta": meta,
        "power": power,
        "probes": probes,
        "outlets": outlets,
        "inputs": inputs,
    }


# Allow config_flow to reuse fetching/parsing for "Test Connection"
__all__ = ["ApexDataUpdateCoordinator", "async_fetch_status", "_parse_status_xml", "_parse_status_json"]
