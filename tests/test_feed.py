# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apex.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_USERNAME, DOMAIN

from .conftest import APEX_IP, BASE_URL, HOST_URL, UID_BASE, load_fixture

FEED_BUTTONS = ["a", "b", "c", "d", "cancel"]


def _status_json(feed: dict | None = None) -> str:
    doc = json.loads(load_fixture("status.json"))
    if feed is not None:
        doc["istat"]["feed"] = feed
    return json.dumps(doc)


async def _setup(hass: HomeAssistant, aioclient_mock, *, password: str = "secret", status: str | None = None):
    aioclient_mock.get(f"{BASE_URL}/status.json", text=status or _status_json())
    data = {CONF_IP_ADDRESS: APEX_IP, CONF_USERNAME: "admin", CONF_PASSWORD: password}
    entry = MockConfigEntry(domain=DOMAIN, title=f"Apex @ {APEX_IP}", data=data, unique_id=APEX_IP)
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def _press(hass: HomeAssistant, key: str) -> None:
    entity_id = _entity_id(hass, "button", f"{UID_BASE}_feed_{key}")
    await hass.services.async_call(BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True)


def _puts(aioclient_mock) -> list[tuple[str, dict]]:
    return [(str(c[1]).removeprefix(HOST_URL), c[2]) for c in aioclient_mock.mock_calls if c[0] == "PUT"]


async def test_buttons_need_a_password(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, password="")
    assert all(_entity_id(hass, "button", f"{UID_BASE}_feed_{k}") is None for k in FEED_BUTTONS)


async def test_buttons_with_password(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    assert all(_entity_id(hass, "button", f"{UID_BASE}_feed_{k}") for k in FEED_BUTTONS)


@pytest.mark.parametrize(
    ("key", "path", "body"),
    [
        ("a", "/rest/status/feed/1", {"name": 1, "active": 1}),
        ("b", "/rest/status/feed/2", {"name": 2, "active": 1}),
        ("c", "/rest/status/feed/3", {"name": 3, "active": 1}),
        ("d", "/rest/status/feed/4", {"name": 4, "active": 1}),
        ("cancel", "/rest/status/feed/0", {"name": 0, "active": 0}),
    ],
)
async def test_press_sends_feed_command(hass: HomeAssistant, aioclient_mock, key, path, body) -> None:
    aioclient_mock.post(f"{HOST_URL}/rest/login", json={"connect.sid": "sid-1"})
    aioclient_mock.put(f"{HOST_URL}{path}", json={**body, "errorCode": 0, "errorMessage": ""})
    await _setup(hass, aioclient_mock)

    await _press(hass, key)

    assert _puts(aioclient_mock) == [(path, body)]


async def test_press_with_wrong_password(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(f"{HOST_URL}/rest/login", status=401)
    await _setup(hass, aioclient_mock)

    with pytest.raises(HomeAssistantError, match="password"):
        await _press(hass, "a")
    assert _puts(aioclient_mock) == []


async def test_press_when_apex_unreachable(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(f"{HOST_URL}/rest/login", json={"connect.sid": "sid-1"})
    aioclient_mock.put(f"{HOST_URL}/rest/status/feed/0", status=500)
    await _setup(hass, aioclient_mock)

    with pytest.raises(HomeAssistantError, match="cancel the feed"):
        await _press(hass, "cancel")


@pytest.mark.parametrize(
    ("feed", "state", "remaining"),
    [
        ({"name": 0, "active": 0}, "Off", 0),
        ({"name": 1, "active": 299}, "A", 299),
        ({"name": 4, "active": 12}, "D", 12),
        # what the Apex reports after a bogus "feed 0" start
        ({"name": 256, "active": 61444}, STATE_UNKNOWN, 0),
    ],
)
async def test_feed_mode_sensor(hass: HomeAssistant, aioclient_mock, feed, state, remaining) -> None:
    await _setup(hass, aioclient_mock, password="", status=_status_json(feed))

    s = hass.states.get(_entity_id(hass, "sensor", f"{UID_BASE}_feed_mode"))
    assert s.state == state
    assert s.attributes["remaining_seconds"] == remaining
    assert s.attributes["options"] == ["Off", "A", "B", "C", "D"]


async def test_no_feed_mode_sensor_on_xml_only_apex(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", status=404)
    aioclient_mock.get(f"{BASE_URL}/status.xml", text=load_fixture("status.xml"))
    await _setup(hass, aioclient_mock, password="")

    assert _entity_id(hass, "sensor", f"{UID_BASE}_feed_mode") is None


async def test_feed_mode_updates_right_after_press(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(f"{HOST_URL}/rest/login", json={"connect.sid": "sid-1"})
    aioclient_mock.put(f"{HOST_URL}/rest/status/feed/1", json={"name": 1, "active": 1, "errorCode": 0})
    await _setup(hass, aioclient_mock)
    sensor = _entity_id(hass, "sensor", f"{UID_BASE}_feed_mode")
    assert hass.states.get(sensor).state == "Off"

    # a refresh was just requested (as during startup), so a debounced one would wait
    await hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]["coordinator"].async_request_refresh()
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{HOST_URL}/rest/login", json={"connect.sid": "sid-1"})
    aioclient_mock.put(f"{HOST_URL}/rest/status/feed/1", json={"name": 1, "active": 1, "errorCode": 0})
    aioclient_mock.get(f"{BASE_URL}/status.json", text=_status_json({"name": 1, "active": 300}))

    await _press(hass, "a")

    assert hass.states.get(sensor).state == "A"
    assert hass.states.get(sensor).attributes["remaining_seconds"] == 300
