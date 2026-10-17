# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apex import coordinator as coordinator_module
from custom_components.apex.const import CONF_IP_ADDRESS, DOMAIN

from .conftest import APEX_IP, BASE_URL, UID_BASE, load_fixture


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, title=f"Apex @ {APEX_IP}", data={CONF_IP_ADDRESS: APEX_IP}, unique_id=APEX_IP)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    # the fixture Apex reports °F; keep HA from converting probe values
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, platform: str, unique_id: str):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id, f"no {platform} entity with unique_id {unique_id}"
    return hass.states.get(entity_id)


def _entities(hass: HomeAssistant, entry: MockConfigEntry, platform: str) -> list[er.RegistryEntry]:
    return [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) if e.domain == platform]


def _requested(aioclient_mock, endpoint: str) -> int:
    return sum(1 for call in aioclient_mock.mock_calls if str(call[1]).endswith(endpoint))


async def test_setup_from_status_json(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))
    entry = _entry()
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert _requested(aioclient_mock, "status.xml") == 0

    # 26 digital inputs and the Apex alarm
    inputs = [e for e in _entities(hass, entry, "binary_sensor") if "_input_" in e.unique_id]
    assert len(inputs) == 26
    assert _state(hass, "binary_sensor", f"{UID_BASE}_alarm").state == STATE_OFF
    sw1 = _state(hass, "binary_sensor", f"{UID_BASE}_input_base_i1")
    assert sw1.state == STATE_OFF
    assert sw1.attributes["apex_state"] == "OPEN"
    assert sw1.attributes["raw_value"] == 0
    swx3_1 = _state(hass, "binary_sensor", f"{UID_BASE}_input_3_i1")
    assert swx3_1.state == STATE_ON
    assert swx3_1.attributes["apex_state"] == "CLOSED"

    # existing xml-era entities keep their unique ids
    assert float(_state(hass, "sensor", f"{UID_BASE}_tmp").state) == 78.6
    assert _state(hass, "switch", f"{UID_BASE}_outlet_9").state == STATE_ON
    assert _state(hass, "sensor", f"{UID_BASE}_power_failed").state == "10/04/2026 15:04:52"


async def test_input_state_follows_apex(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))
    entry = _entry()
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    doc = json.loads(load_fixture("status.json"))
    for i in doc["istat"]["inputs"]:
        if i["did"] == "base_I1":
            i["value"] = 200
        if i["did"] == "base_I2":
            i["value"] = 7
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}/status.json", text=json.dumps(doc))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, "binary_sensor", f"{UID_BASE}_input_base_i1").state == STATE_ON
    assert _state(hass, "binary_sensor", f"{UID_BASE}_input_base_i2").state == STATE_UNKNOWN


async def test_falls_back_to_status_xml(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", status=404)
    aioclient_mock.get(f"{BASE_URL}/status.xml", text=load_fixture("status.xml"))
    entry = _entry()
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert float(_state(hass, "sensor", f"{UID_BASE}_tmp").state) == 78.6
    assert _state(hass, "switch", f"{UID_BASE}_outlet_9").state == STATE_ON
    assert _entities(hass, entry, "binary_sensor") == []

    # status.json isn't requested again once it's known to be missing
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    xml_before = _requested(aioclient_mock, "status.xml")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _requested(aioclient_mock, "status.json") == 1
    assert _requested(aioclient_mock, "status.xml") == xml_before + 1


@pytest.mark.parametrize(
    "json_response",
    [{"status": 403}, {"status": 500}, {"text": "<html>Not Found</html>"}],
    ids=["403", "500", "html-page"],
)
async def test_unusable_status_json_falls_back_to_xml(hass: HomeAssistant, aioclient_mock, json_response) -> None:
    """Older firmware may not answer status.json with a clean 404."""
    aioclient_mock.get(f"{BASE_URL}/status.json", **json_response)
    aioclient_mock.get(f"{BASE_URL}/status.xml", text=load_fixture("status.xml"))
    entry = _entry()
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert float(_state(hass, "sensor", f"{UID_BASE}_tmp").state) == 78.6


@pytest.mark.parametrize(
    "exc", [aiohttp.ClientError("unreachable"), asyncio.TimeoutError()], ids=["connection", "timeout"]
)
async def test_setup_retries_when_apex_unreachable(hass: HomeAssistant, aioclient_mock, exc) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", exc=exc)
    entry = _entry()
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    # an unreachable Apex is not old firmware: don't fall back to xml
    assert _requested(aioclient_mock, "status.xml") == 0


async def test_status_json_hiccup_is_not_permanent(hass: HomeAssistant, aioclient_mock) -> None:
    """A JSON-capable Apex that fails status.json once goes back to it on the next poll."""
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))
    entry = _entry()
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    sw1 = f"{UID_BASE}_input_base_i1"

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}/status.json", status=404)
    aioclient_mock.get(f"{BASE_URL}/status.xml", text=load_fixture("status.xml"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data["source"] == "xml"
    assert _state(hass, "binary_sensor", sw1).state == STATE_UNAVAILABLE
    assert float(_state(hass, "sensor", f"{UID_BASE}_tmp").state) == 78.6

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data["source"] == "json"
    assert _state(hass, "binary_sensor", sw1).state == STATE_OFF


async def test_xml_only_apex_checks_for_status_json_hourly(
    hass: HomeAssistant, aioclient_mock, monkeypatch
) -> None:
    """After a firmware update adds status.json, it's picked up without reloading."""
    now = 1000.0
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: now)
    aioclient_mock.get(f"{BASE_URL}/status.json", status=404)
    aioclient_mock.get(f"{BASE_URL}/status.xml", text=load_fixture("status.xml"))
    entry = _entry()
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    json_before = _requested(aioclient_mock, "status.json")

    now += coordinator_module.JSON_RETRY_SECONDS - 1
    await coordinator.async_refresh()
    assert _requested(aioclient_mock, "status.json") == json_before

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))
    now += 2
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.data["source"] == "json"
    assert _state(hass, "binary_sensor", f"{UID_BASE}_input_3_i1").state == STATE_ON


async def test_unload(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))
    entry = _entry()
    await _setup(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]
