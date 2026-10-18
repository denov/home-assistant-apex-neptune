# SPDX-License-Identifier: AGPL-3.0-or-later
"""Output control, services, REST extras (Trident, config, firmware) and read-only mode."""
from __future__ import annotations

import json

import aiohttp
import pytest
from yarl import URL
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN, SERVICE_SELECT_OPTION
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.apex import coordinator as coordinator_module
from custom_components.apex.const import (
    CONF_ENABLE_PER_OUTLET_POWER,
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_READ_ONLY,
    CONF_TEMP_UNIT,
    CONF_USERNAME,
    DOMAIN,
)

from .conftest import APEX_IP, BASE_URL, HOST_URL, UID_BASE, load_fixture

OK = {"errorCode": 0, "errorMessage": ""}


def _status_json(edit=None) -> str:
    doc = json.loads(load_fixture("status.json"))
    if edit:
        edit(doc["istat"])
    return json.dumps(doc)


def _mock_rest(aioclient_mock, *, login_status: int = 200) -> None:
    if login_status == 200:
        aioclient_mock.post(f"{HOST_URL}/rest/login", json={"connect.sid": "sid-1"})
    else:
        aioclient_mock.post(f"{HOST_URL}/rest/login", status=login_status)
    aioclient_mock.get(f"{HOST_URL}/rest/status", text=load_fixture("rest_status.json"))
    aioclient_mock.get(f"{HOST_URL}/rest/config", text=load_fixture("rest_config.json"))


async def _setup(
    hass: HomeAssistant,
    aioclient_mock,
    *,
    password: str = "secret",
    options: dict | None = None,
    status: str | None = None,
    rest: bool = True,
    metric: bool = False,
) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE_URL}/status.json", text=status or _status_json())
    if rest:
        _mock_rest(aioclient_mock)
    data = {CONF_IP_ADDRESS: APEX_IP, CONF_USERNAME: "admin", CONF_PASSWORD: password}
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options or {}, unique_id=APEX_IP)
    # the fixture Apex reports °F; mL would show as fl. oz. in US units
    hass.config.units = METRIC_SYSTEM if metric else US_CUSTOMARY_SYSTEM
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


def _state(hass: HomeAssistant, platform: str, unique_id: str):
    entity_id = _entity_id(hass, platform, unique_id)
    assert entity_id, f"no {platform} entity with unique_id {unique_id}"
    return hass.states.get(entity_id)


def _calls(aioclient_mock, method: str, path: str) -> list:
    return [c for c in aioclient_mock.mock_calls if c[0] == method and str(c[1]) == f"{HOST_URL}{path}"]


def _puts(aioclient_mock) -> list[tuple[str, dict]]:
    return [(str(c[1]).removeprefix(HOST_URL), c[2]) for c in aioclient_mock.mock_calls if c[0] == "PUT"]


def _oconf(did: str) -> dict:
    return next(o for o in json.loads(load_fixture("rest_config.json"))["oconf"] if o["did"] == did)


# --- naming ---------------------------------------------------------------


async def test_entities_are_named_after_the_device(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, password="", rest=False)

    tmp = _state(hass, "sensor", f"{UID_BASE}_tmp")
    assert tmp.entity_id == "sensor.apex_temp"
    assert tmp.attributes["friendly_name"] == "Apex Temp"
    entity = er.async_get(hass).async_get(tmp.entity_id)
    assert entity.has_entity_name


# --- REST extras ------------------------------------------------------------


async def test_trident_levels(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, metric=True)

    a = _state(hass, "sensor", f"{UID_BASE}_trident_5_reagent_a")
    assert a.attributes["friendly_name"] == "Apex TRI Reagent A"
    assert float(a.state) == 111.3
    assert a.attributes["unit_of_measurement"] == "mL"
    # what the Apex dashboard shows: floor(111.28 / 1.9)
    assert a.attributes["tests_remaining"] == 58
    assert float(_state(hass, "sensor", f"{UID_BASE}_trident_5_reagent_b").state) == 73.2
    assert float(_state(hass, "sensor", f"{UID_BASE}_trident_5_reagent_c").state) == 76.5

    waste = _state(hass, "sensor", f"{UID_BASE}_trident_5_waste")
    assert float(waste.state) == 997.4
    assert waste.attributes["container_ml"] == 1610.0
    assert waste.attributes["percent_full"] == 62.0


async def test_trident_np_reagents_are_numbered(hass: HomeAssistant, aioclient_mock) -> None:
    status = json.loads(load_fixture("rest_status.json"))
    for m in status["modules"]:
        if m["hwtype"] == "TRI":
            m["hwtype"] = "TNP"
    aioclient_mock.get(f"{HOST_URL}/rest/status", text=json.dumps(status))
    await _setup(hass, aioclient_mock)

    one = _state(hass, "sensor", f"{UID_BASE}_trident_5_reagent_1")
    # 3.652 mL per test on a Trident NP
    assert one.attributes["tests_remaining"] == 30


async def test_no_trident_without_password(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, password="", rest=False)
    assert _entity_id(hass, "sensor", f"{UID_BASE}_trident_5_reagent_a") is None
    assert _entity_id(hass, "update", f"{UID_BASE}_firmware") is None
    assert not [c for c in aioclient_mock.mock_calls if "/rest/" in str(c[1])]


async def test_rejected_login_backs_off(hass: HomeAssistant, aioclient_mock) -> None:
    """Retrying a bad password every poll can lock the Apex's web interface."""
    aioclient_mock.get(f"{BASE_URL}/status.json", text=_status_json())
    _mock_rest(aioclient_mock, login_status=401)
    data = {CONF_IP_ADDRESS: APEX_IP, CONF_USERNAME: "admin", CONF_PASSWORD: "wrong"}
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=APEX_IP)
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # monitoring still works
    assert float(_state(hass, "sensor", f"{UID_BASE}_tmp").state) == 78.6
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert len(_calls(aioclient_mock, "POST", "/rest/login")) == 1

    # and Home Assistant asks for the password, once
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == "reauth"
    assert flow["context"]["entry_id"] == entry.entry_id


async def test_extras_refresh_slowly(hass: HomeAssistant, aioclient_mock, monkeypatch) -> None:
    entry = await _setup(hass, aioclient_mock)
    coordinator = entry.runtime_data.coordinator

    await coordinator.async_refresh()
    assert len(_calls(aioclient_mock, "GET", "/rest/config")) == 1

    clock = coordinator_module.time.monotonic() + coordinator_module.EXTRAS_SECONDS + 1
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: clock)
    await coordinator.async_refresh()
    assert len(_calls(aioclient_mock, "GET", "/rest/config")) == 2
    # the session is reused
    assert len(_calls(aioclient_mock, "POST", "/rest/login")) == 1


async def test_firmware_update_entity(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    fw = _state(hass, "update", f"{UID_BASE}_firmware")
    assert fw.state == STATE_OFF
    assert fw.attributes["installed_version"] == "5.15_6B26"
    assert fw.attributes["latest_version"] == "5.15_6B26"


async def test_firmware_lite_version_is_not_an_update(hass: HomeAssistant, aioclient_mock) -> None:
    """An A3 reports 5.11L_6A23 in its status but 5.11_6A23 as the latest firmware."""
    config = json.loads(load_fixture("rest_config.json"))
    config["nconf"]["latestFirmware"] = "5.11_6A23"
    aioclient_mock.get(f"{HOST_URL}/rest/config", text=json.dumps(config))

    def lite(istat):
        istat["software"] = "5.11L_6A23"

    await _setup(hass, aioclient_mock, status=_status_json(lite))
    fw = _state(hass, "update", f"{UID_BASE}_firmware")
    assert fw.state == STATE_OFF
    assert fw.attributes["installed_version"] == "5.11_6A23"


async def test_firmware_install(hass: HomeAssistant, aioclient_mock) -> None:
    config = json.loads(load_fixture("rest_config.json"))
    config["nconf"]["latestFirmware"] = "5.16_1A27"
    aioclient_mock.get(f"{HOST_URL}/rest/config", text=json.dumps(config))
    aioclient_mock.put(f"{HOST_URL}/rest/config/nconf", json=OK)
    await _setup(hass, aioclient_mock)
    fw = _state(hass, "update", f"{UID_BASE}_firmware")
    assert fw.state == STATE_ON

    entity = hass.data["entity_components"]["update"].get_entity(fw.entity_id)
    with pytest.raises(HomeAssistantError, match="still on"):
        # the mocked Apex never changes version
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("custom_components.apex.update.INSTALL_TIMEOUT_SECONDS", 0)
            await entity.async_install(None, False)

    ((path, body),) = _puts(aioclient_mock)
    assert path == "/rest/config/nconf"
    assert body["updateFirmware"] is True
    assert body["hostname"] == "apex"


async def test_dos_volume_and_variable_setting(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, metric=True)

    dos = _state(hass, "sensor", f"{UID_BASE}_output_22_volume_left")
    assert dos.attributes["friendly_name"] == "Apex DOS_DEL Volume Left"
    assert float(dos.state) == 0
    assert dos.attributes["capacity_ml"] == 2000

    var = _state(hass, "sensor", f"{UID_BASE}_output_0_setting")
    assert var.state == "PF1"
    assert var.attributes["program"] == "Set PF1\n"


async def test_pump_intensity(hass: HomeAssistant, aioclient_mock) -> None:
    def add_pump(istat):
        istat["outputs"].append(
            {"status": ["AON", "75", "OK", ""], "intensity": 75, "name": "Sicce_Return", "gid": "",
             "type": "iotaPump|Sicce|Syncra", "ID": 19, "did": "3_9"}
        )

    await _setup(hass, aioclient_mock, password="", rest=False, status=_status_json(add_pump))
    assert float(_state(hass, "sensor", f"{UID_BASE}_output_19_intensity").state) == 75


async def test_per_outlet_energy(hass: HomeAssistant, aioclient_mock) -> None:
    def add_eb832(istat):
        istat["inputs"] += [
            {"did": "2_1W", "type": "pwr", "name": "Return_2_1W", "value": 40},
            {"did": "2_2W", "type": "pwr", "name": "Skimmer_2_2W", "value": 20},
        ]

    await _setup(
        hass, aioclient_mock, password="", rest=False, status=_status_json(add_eb832),
        options={CONF_ENABLE_PER_OUTLET_POWER: True},
    )
    power = _state(hass, "sensor", f"{UID_BASE}_return_2_1w")
    assert power.attributes["friendly_name"] == "Apex Return_2_1W"
    assert power.attributes["unit_of_measurement"] == "W"
    energy = _state(hass, "sensor", f"{UID_BASE}_return_2_1w_energy")
    assert energy.attributes["unit_of_measurement"] == "kWh"


# --- alarm ------------------------------------------------------------------


async def test_alarm(hass: HomeAssistant, aioclient_mock) -> None:
    def alarming(istat):
        for o in istat["outputs"]:
            if o["did"] == "base_email":
                o["status"][0] = "AON"
            if o["did"] == "4_1":
                o["status"][2] = "Volt"

    await _setup(hass, aioclient_mock, password="", rest=False, status=_status_json(alarming))
    alarm = _state(hass, "binary_sensor", f"{UID_BASE}_alarm")
    assert alarm.state == STATE_ON
    assert alarm.attributes["device_class"] == "problem"
    assert alarm.attributes["active_alerts"] == ["EmailAlarm"]
    assert alarm.attributes["output_errors"] == {"DOS_DEL": "Volt"}


# --- control ----------------------------------------------------------------


async def test_mode_select(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.put(f"{HOST_URL}/rest/status/outputs/2_2", json=OK)
    await _setup(hass, aioclient_mock)

    assert _state(hass, "select", f"{UID_BASE}_outlet_9_mode").state == "on"  # ON
    assert _state(hass, "select", f"{UID_BASE}_outlet_10_mode").state == "auto"  # AOF
    assert _state(hass, "select", f"{UID_BASE}_outlet_0_mode").state == "auto"  # PF1 profile

    entity_id = _entity_id(hass, "select", f"{UID_BASE}_outlet_9_mode")
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: entity_id, "option": "auto"}, blocking=True
    )
    assert _puts(aioclient_mock) == [
        ("/rest/status/outputs/2_2", {"did": "2_2", "status": ["AUTO", "", "OK", ""], "type": "outlet"})
    ]


@pytest.mark.parametrize(("service", "mode"), [(SERVICE_TURN_ON, "ON"), (SERVICE_TURN_OFF, "OFF")])
async def test_switch_control(hass: HomeAssistant, aioclient_mock, service, mode) -> None:
    aioclient_mock.put(f"{HOST_URL}/rest/status/outputs/2_2", json=OK)
    await _setup(hass, aioclient_mock)

    entity_id = _entity_id(hass, "switch", f"{UID_BASE}_outlet_9")
    await hass.services.async_call(SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    assert _puts(aioclient_mock) == [
        ("/rest/status/outputs/2_2", {"did": "2_2", "status": [mode, "", "OK", ""], "type": "outlet"})
    ]


async def test_switch_needs_password(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, password="", rest=False)
    entity_id = _entity_id(hass, "switch", f"{UID_BASE}_outlet_9")
    with pytest.raises(HomeAssistantError, match="password"):
        await hass.services.async_call(SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    assert _entity_id(hass, "select", f"{UID_BASE}_outlet_9_mode") is None


async def test_read_only(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, options={CONF_READ_ONLY: True})

    # still reads the REST API...
    assert _entity_id(hass, "sensor", f"{UID_BASE}_trident_5_reagent_a")
    fw = _state(hass, "update", f"{UID_BASE}_firmware")
    assert not fw.attributes["supported_features"]
    # ...but nothing changes the Apex
    assert _entity_id(hass, "select", f"{UID_BASE}_outlet_9_mode") is None
    assert _entity_id(hass, "button", f"{UID_BASE}_feed_a") is None
    assert not hass.services.has_service(DOMAIN, "set_output")
    entity_id = _entity_id(hass, "switch", f"{UID_BASE}_outlet_9")
    with pytest.raises(HomeAssistantError, match="read-only"):
        await hass.services.async_call(SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    assert _puts(aioclient_mock) == []


async def test_set_output_service(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.put(f"{HOST_URL}/rest/status/outputs/base_Var1", json=OK)
    await _setup(hass, aioclient_mock)

    entity_id = _entity_id(hass, "select", f"{UID_BASE}_outlet_0_mode")
    await hass.services.async_call(DOMAIN, "set_output", {ATTR_ENTITY_ID: entity_id, "setting": "off"}, blocking=True)
    assert _puts(aioclient_mock) == [
        ("/rest/status/outputs/base_Var1", {"did": "base_Var1", "status": ["OFF", "", "OK", ""], "type": "outlet"})
    ]


async def test_set_variable_service(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.put(f"{HOST_URL}/rest/config/oconf/base_Var1", json=OK)
    await _setup(hass, aioclient_mock)

    entity_id = _entity_id(hass, "select", f"{UID_BASE}_outlet_0_mode")
    await hass.services.async_call(DOMAIN, "set_variable", {ATTR_ENTITY_ID: entity_id, "code": "Set 75"}, blocking=True)

    ((path, body),) = _puts(aioclient_mock)
    assert path == "/rest/config/oconf/base_Var1"
    # everything else is sent back as the Apex had it
    assert body == {**_oconf("base_Var1"), "ctype": "Advanced", "prog": "Set 75\n"}


async def test_set_dos_rate_service(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.put(f"{HOST_URL}/rest/config/oconf/4_1", json=OK)
    aioclient_mock.put(f"{HOST_URL}/rest/config/pconf/11", json=OK)
    await _setup(hass, aioclient_mock)

    entity_id = _entity_id(hass, "select", f"{UID_BASE}_outlet_22_mode")
    await hass.services.async_call(
        DOMAIN, "set_dos_rate", {ATTR_ENTITY_ID: entity_id, "profile_id": 11, "rate": 1.2}, blocking=True
    )

    puts = _puts(aioclient_mock)
    assert [p for p, _ in puts] == ["/rest/config/oconf/4_1", "/rest/config/pconf/11", "/rest/config/oconf/4_1"]
    assert puts[0][1]["prog"] == "Set OFF\n"
    # 1.2 mL/min: the 7 mL/min speed (index 5) forward (16)
    assert puts[1][1] == {"name": "Dose_4_1", "ID": 11, "type": "dose", "data": {"mode": 21, "amount": 1.2, "time": 60, "count": 255}}
    assert puts[2][1]["prog"] == "Set Dose_4_1\n"


async def test_set_dos_rate_only_for_dos(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    entity_id = _entity_id(hass, "select", f"{UID_BASE}_outlet_9_mode")
    with pytest.raises(ServiceValidationError, match="DOS"):
        await hass.services.async_call(
            DOMAIN, "set_dos_rate", {ATTR_ENTITY_ID: entity_id, "profile_id": 11, "rate": 1.2}, blocking=True
        )
    assert _puts(aioclient_mock) == []


async def test_two_apexes_keep_their_own_services(hass: HomeAssistant, aioclient_mock) -> None:
    """A service call goes to the Apex of the targeted entity (upstream issue: did alone was ambiguous)."""
    aioclient_mock.put(f"{HOST_URL}/rest/status/outputs/2_2", json=OK)
    aioclient_mock.put("http://5.6.7.8/rest/status/outputs/2_2", json=OK)
    await _setup(hass, aioclient_mock)

    other = json.loads(load_fixture("status.json"))
    other["istat"]["serial"] = "AC5:99999"
    aioclient_mock.get("http://5.6.7.8/cgi-bin/status.json", text=json.dumps(other))
    aioclient_mock.post("http://5.6.7.8/rest/login", json={"connect.sid": "sid-2"})
    data = {CONF_IP_ADDRESS: "5.6.7.8", CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}
    entry2 = MockConfigEntry(domain=DOMAIN, data=data, unique_id="5.6.7.8")
    entry2.add_to_hass(hass)
    await hass.config_entries.async_setup(entry2.entry_id)
    await hass.async_block_till_done()

    entity_id = _entity_id(hass, "select", "ac5_99999_outlet_9_mode")
    await hass.services.async_call(DOMAIN, "set_output", {ATTR_ENTITY_ID: entity_id, "setting": "AUTO"}, blocking=True)
    assert [str(c[1]) for c in aioclient_mock.mock_calls if c[0] == "PUT"] == ["http://5.6.7.8/rest/status/outputs/2_2"]


# --- status page login -------------------------------------------------------


def _record_status_auth(aioclient_mock, monkeypatch, *, required: bool) -> list:
    """The auth sent with each status page request; with required, the Apex answers 401 without it."""
    sent: list = []
    match = aioclient_mock.match_request

    async def spy(method, url, *, auth=None, **kwargs):
        if "/cgi-bin/" in str(url):
            sent.append(auth)
            if required and auth is None:
                return AiohttpClientMockResponse(method, URL(url), status=401)
        return await match(method, url, auth=auth, **kwargs)

    # before the first request creates the session that binds it
    monkeypatch.setattr(aioclient_mock, "match_request", spy)
    return sent


async def test_status_is_read_without_the_password(hass: HomeAssistant, aioclient_mock, monkeypatch) -> None:
    """Status pages are plain HTTP: don't send the password when the Apex doesn't ask for it."""
    sent = _record_status_auth(aioclient_mock, monkeypatch, required=False)
    entry = await _setup(hass, aioclient_mock)
    await entry.runtime_data.coordinator.async_refresh()
    assert sent == [None, None]


async def test_password_sent_when_status_needs_it(hass: HomeAssistant, aioclient_mock, monkeypatch) -> None:
    sent = _record_status_auth(aioclient_mock, monkeypatch, required=True)
    entry = await _setup(hass, aioclient_mock)
    assert float(_state(hass, "sensor", f"{UID_BASE}_tmp").state) == 78.6

    # status.json, then status.xml without it; status.json with it
    auth = aiohttp.BasicAuth("admin", "secret")
    assert sent == [None, None, auth]
    # from then on, straight away
    await entry.runtime_data.coordinator.async_refresh()
    assert sent[3:] == [auth]


async def test_firmware_install_finishes_when_the_version_changes(
    hass: HomeAssistant, aioclient_mock, monkeypatch
) -> None:
    config = json.loads(load_fixture("rest_config.json"))
    config["nconf"]["latestFirmware"] = "5.16_1A27"
    aioclient_mock.get(f"{HOST_URL}/rest/config", text=json.dumps(config))

    def updated(istat):
        istat["software"] = "5.16_1A27"

    async def install(method, url, data):
        # the Apex comes back on the new version
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}/status.json", text=_status_json(updated))
        _mock_rest(aioclient_mock)
        return AiohttpClientMockResponse(method, url, json=OK)

    aioclient_mock.put(f"{HOST_URL}/rest/config/nconf", side_effect=install)
    await _setup(hass, aioclient_mock)
    monkeypatch.setattr("custom_components.apex.update.INSTALL_POLL_SECONDS", 0)
    fw = _state(hass, "update", f"{UID_BASE}_firmware")
    assert fw.state == STATE_ON

    entity = hass.data["entity_components"]["update"].get_entity(fw.entity_id)
    await entity.async_install(None, False)
    await hass.async_block_till_done()

    fw = _state(hass, "update", f"{UID_BASE}_firmware")
    assert fw.attributes["installed_version"] == "5.16_1A27"
    assert fw.attributes["in_progress"] is False


async def test_output_without_a_device_id(hass: HomeAssistant, aioclient_mock) -> None:
    def no_did(istat):
        for o in istat["outputs"]:
            if o["did"] == "2_2":
                del o["did"]

    await _setup(hass, aioclient_mock, status=_status_json(no_did))
    entity_id = _entity_id(hass, "switch", f"{UID_BASE}_outlet_9")
    with pytest.raises(HomeAssistantError, match="device id"):
        await hass.services.async_call(SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    assert _puts(aioclient_mock) == []


@pytest.mark.parametrize(("saved", "unit"), [("F", "°F"), ("C", "°C"), ("", "°F"), ("c", "°C")])
async def test_temp_unit_saved_by_older_versions(hass: HomeAssistant, aioclient_mock, saved, unit) -> None:
    await _setup(hass, aioclient_mock, password="", rest=False, options={CONF_TEMP_UNIT: saved})
    entity = hass.data["entity_components"]["sensor"].get_entity(_entity_id(hass, "sensor", f"{UID_BASE}_tmp"))
    # what the Apex reports in, before Home Assistant converts it to the unit system
    assert entity.native_unit_of_measurement == unit


# --- diagnostics --------------------------------------------------------------


async def test_diagnostics_redact_credentials(hass: HomeAssistant, aioclient_mock) -> None:
    from custom_components.apex.diagnostics import async_get_config_entry_diagnostics

    entry = await _setup(hass, aioclient_mock)
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["rest_api"] is True
    assert diag["data"]["source"] == "json"
    assert diag["data"]["probes"]["Tmp"]["value"] == 78.6
    assert diag["entry"]["data"][CONF_PASSWORD] == "**REDACTED**"
    nconf = diag["data"]["config"]["nconf"]
    assert nconf["password"] == nconf["emailUser"] == nconf["ipaddr"] == "**REDACTED**"
    assert nconf["latestFirmware"] == "5.15_6B26"
    text = json.dumps(diag, default=str)
    for secret in ("secret", "AC5:12345", APEX_IP, "name@example.com", "mail.example.com"):
        assert secret not in text
