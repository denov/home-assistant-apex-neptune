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

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_TIMEOUT, ApexSettings
from .rest import ApexAuthError, ApexRestClient

_LOGGER = logging.getLogger(__name__)

# status.json reports digital inputs as 0 (OPEN) or 200 (CLOSED); unconnected inputs read OPEN.
DIGITAL_OPEN = 0
DIGITAL_CLOSED = 200

# How often to look for status.json again on an Apex that only serves status.xml.
JSON_RETRY_SECONDS = 3600

# How often to read /rest/status (modules) and /rest/config, which change slowly.
EXTRAS_SECONDS = 300
# After the Apex rejects the login, wait this long before trying again: repeated
# failed logins can lock the Apex's local web interface.
AUTH_RETRY_SECONDS = 1800

# what the Apex answers a request that needs the password
AUTH_STATUSES = (401, 403)

# Entities go unavailable after this many failed polls in a row, rather than showing
# the last readings as current while the Apex is offline.
UNAVAILABLE_AFTER_FAILURES = 3


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


async def _async_fetch_status_once(
    session: aiohttp.ClientSession,
    base_url: str,
    auth: aiohttp.BasicAuth | None,
    try_json: bool,
) -> dict[str, Any]:
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


async def async_fetch_status(
    session: aiohttp.ClientSession,
    base_url: str,
    *,
    auth: aiohttp.BasicAuth | None = None,
    auth_first: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT,
    try_json: bool = True,
) -> dict[str, Any]:
    """Fetch status.json, or status.xml when status.json isn't usable (data["source"] says which).

    Older firmware may answer status.json with any HTTP error or a non-JSON page, so those fall
    back to status.xml. Timeouts and connection errors mean the Apex is unreachable and are raised,
    as are errors from status.xml: asyncio.TimeoutError, aiohttp.ClientError or ET.ParseError.
    One timeout covers every request.

    The status pages normally need no login, and this is plain HTTP, so auth is only sent
    when the Apex asks for it (or auth_first); data["auth"] says whether it was needed.
    """
    async with asyncio.timeout(timeout_seconds):
        if auth_first and auth is not None:
            return {**await _async_fetch_status_once(session, base_url, auth, try_json), "auth": True}
        try:
            return await _async_fetch_status_once(session, base_url, None, try_json)
        except aiohttp.ClientResponseError as err:
            if auth is None or err.status not in AUTH_STATUSES:
                raise
        return {**await _async_fetch_status_once(session, base_url, auth, try_json), "auth": True}


class ApexDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches and parses Apex status.json (or status.xml) into a shared data dict for entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry | None,
        settings: ApexSettings,
        rest: ApexRestClient | None = None,
    ) -> None:
        # with a password: also read modules and config through the REST API
        self.rest = rest
        self.settings = settings
        self._auth = aiohttp.BasicAuth(settings.username, settings.password) if settings.password else None
        self._auth_needed = False
        # polls in a row that failed; entities go unavailable after a few
        self.failed_polls = 0
        # Stop asking for status.json only on an Apex where it has never worked,
        # and even then look again occasionally (e.g. after a firmware update).
        self._json_seen = False
        self._json_retry_at: float | None = None
        self._json_failing = False
        self._extras: dict[str, Any] = {"modules": [], "config": None}
        self._extras_at: float | None = None
        self._extras_failing = False

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"Apex @ {settings.host}",
            update_interval=timedelta(seconds=settings.scan_interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try_json = self._json_retry_at is None or time.monotonic() >= self._json_retry_at

        try:
            data = await async_fetch_status(
                async_get_clientsession(self.hass),
                self.settings.status_url,
                auth=self._auth,
                auth_first=self._auth_needed,
                timeout_seconds=self.settings.timeout,
                try_json=try_json,
            )
        except Exception as err:
            self.failed_polls += 1
            if self.failed_polls == UNAVAILABLE_AFTER_FAILURES:
                # the coordinator only tells entities about the first failure in a row
                self.async_update_listeners()
            raise UpdateFailed(f"Error fetching Apex status: {err}") from err
        self.failed_polls = 0
        self._auth_needed = bool(data.pop("auth", False))

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

        if self.rest is not None:
            await self._async_update_extras()
        data.update(self._extras)
        return data

    @property
    def stale(self) -> bool:
        """The last few polls failed, so the readings can't be trusted to be current."""
        return self.failed_polls >= UNAVAILABLE_AFTER_FAILURES

    def async_start_reauth(self) -> None:
        """Have Home Assistant ask for the password again (once, however often it's rejected)."""
        if self.config_entry is not None:
            self.config_entry.async_start_reauth(self.hass)

    def request_extras(self) -> None:
        """Read modules and config again on the next refresh (e.g. after changing a program)."""
        self._extras_at = None

    async def _async_update_extras(self) -> None:
        """Refresh modules and config every EXTRAS_SECONDS; keep the last ones if that fails.

        Monitoring doesn't depend on this, so errors are logged rather than failing the update.
        """
        now = time.monotonic()
        if self._extras_at is not None and now < self._extras_at:
            return
        self._extras_at = now + EXTRAS_SECONDS
        try:
            status = await self.rest.async_get_status()
            config = await self.rest.async_get_config()
        except ApexAuthError as err:
            self._extras_at = now + AUTH_RETRY_SECONDS
            _LOGGER.warning(
                "%s; Trident levels, firmware updates and output programs are unavailable. "
                "Will try again in %s minutes",
                err,
                AUTH_RETRY_SECONDS // 60,
            )
            self._extras_failing = True
            # monitoring doesn't need the password, so ask for it rather than fail the update
            self.async_start_reauth()
            return
        except Exception as err:  # noqa: BLE001 - never let this break monitoring
            if not self._extras_failing:
                _LOGGER.warning("Couldn't read the Apex REST API (%s); will keep trying", err)
            self._extras_failing = True
            return

        if self._extras_failing:
            _LOGGER.info("Apex REST API is readable again")
        self._extras_failing = False
        modules = status.get("modules")
        self._extras = {
            "modules": [m for m in modules if isinstance(m, dict)] if isinstance(modules, list) else [],
            "config": config,
        }


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
        state = (o.findtext("state") or "").strip() or None
        outlets.append(
            {
                "name": name,
                "output_id": (o.findtext("outputID") or "").strip() or None,
                "state": state,
                "device_id": (o.findtext("deviceID") or "").strip() or None,
                # status.xml has only the state
                "type": None,
                "gid": None,
                "status": [state],
                "intensity": None,
            }
        )

    return {
        "source": "xml",
        "meta": meta,
        "power": power,
        "probes": probes,
        "outlets": outlets,
        "inputs": {},  # status.xml doesn't report digital inputs
        "feed": None,  # or the feed cycle
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
    name_counts: dict[str, int] = {}
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
        key = name
        name_counts[name] = name_counts.get(name, 0) + 1
        if key in probes:
            # two inputs with the same name (e.g. two "Leak" probes): tell them apart by device id,
            # else by which one of that name it is
            key = f"{name}_{_str_or_none(i.get('did')) or name_counts[name]}"
        probes[key] = {
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
        if not isinstance(status, list):
            status = []
        intensity = o.get("intensity")
        outlets.append(
            {
                "name": name,
                "output_id": _str_or_none(o.get("ID")),
                "state": _str_or_none(status[0]) if status else None,
                "device_id": _str_or_none(o.get("did")),
                "type": _str_or_none(o.get("type")),
                "gid": _str_or_none(o.get("gid")),
                # status[0] is the state; what follows depends on the type
                # (e.g. intensity %, "OK" or an error such as "Volt", DOS capacity and volume left)
                "status": [_str_or_none(v) for v in status],
                "intensity": intensity if isinstance(intensity, (int, float)) and not isinstance(intensity, bool) else None,
            }
        )

    feed_raw = istat.get("feed")
    feed: dict[str, Any] | None = None
    if isinstance(feed_raw, dict):
        name, active = feed_raw.get("name"), feed_raw.get("active")
        feed = {
            # 0 = no feed, 1-4 = feed A-D
            "name": name if isinstance(name, int) else None,
            # seconds remaining while a feed runs
            "active": active if isinstance(active, int) else None,
        }

    return {
        "source": "json",
        "meta": meta,
        "power": power,
        "probes": probes,
        "outlets": outlets,
        "inputs": inputs,
        "feed": feed,
    }
