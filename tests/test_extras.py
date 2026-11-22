# SPDX-License-Identifier: AGPL-3.0-or-later
"""Modules, Apex Fusion, Wi-Fi, probe calibration, feed durations, and outputs as lights and fans."""
from __future__ import annotations

import json

import pytest
from homeassistant.components.fan import ATTR_PERCENTAGE, DOMAIN as FAN_DOMAIN, SERVICE_SET_PERCENTAGE
from homeassistant.components.light import ATTR_BRIGHTNESS, DOMAIN as LIGHT_DOMAIN
from homeassistant.components.number import ATTR_VALUE, DOMAIN as NUMBER_DOMAIN, SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util.dt import parse_datetime

from custom_components.apex import coordinator as coordinator_module
from custom_components.apex.const import CONF_FAN_OUTPUTS, CONF_LIGHT_OUTPUTS, CONF_READ_ONLY, DOMAIN

from .conftest import BASE_URL, HOST_URL, UID_BASE, load_fixture
from .test_control import OK, _entity_id, _mock_rest, _oconf, _puts, _setup, _state, _status_json


def _rest_config(edit=None) -> str:
    config = json.loads(load_fixture("rest_config.json"))
    if edit:
        edit(config)
    return json.dumps(config)


def _rest_status(edit=None) -> str:
    status = json.loads(load_fixture("rest_status.json"))
    if edit:
        edit(status)
    return json.dumps(status)


def _set_prog(did: str, prog: str):
    def edit(config):
        next(o for o in config["oconf"] if o["did"] == did)["prog"] = prog

    return edit


def _device(hass: HomeAssistant, entry, identifier: str):
    """The entry's device with this identifier, if any."""
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return next((d for d in devices if (DOMAIN, identifier) in d.identifiers), None)


async def _refresh_extras(hass: HomeAssistant, entry, monkeypatch) -> None:
    """Poll as if the REST status and config are due again."""
    clock = coordinator_module.time.monotonic() + coordinator_module.EXTRAS_SECONDS + 1
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: clock)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


# --- AquaBus modules -------------------------------------------------------


async def test_modules_are_devices(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock)

    apex = _device(hass, entry, UID_BASE)
    trident = _device(hass, entry, f"{UID_BASE}_module_5")
    assert trident.name == "TRI"
    assert trident.model == "Trident"
    assert trident.sw_version == "23"
    assert trident.via_device_id == apex.id
    eb8 = _device(hass, entry, f"{UID_BASE}_module_2")
    assert (eb8.name, eb8.model) == ("EB8_DOWN2", "Energy Bar 8")

    connected = _state(hass, "binary_sensor", f"{UID_BASE}_module_2_connected")
    assert connected.state == STATE_ON
    assert connected.attributes["friendly_name"] == "EB8_DOWN2 Connected"
    assert connected.attributes["firmware_status"] == "OK"


async def test_module_disconnected(hass: HomeAssistant, aioclient_mock) -> None:
    def unplug(status):
        next(m for m in status["modules"] if m["abaddr"] == 2)["present"] = False

    aioclient_mock.get(f"{HOST_URL}/rest/status", text=_rest_status(unplug))
    await _setup(hass, aioclient_mock)
    assert _state(hass, "binary_sensor", f"{UID_BASE}_module_2_connected").state == STATE_OFF


async def test_module_errors_are_disabled_by_default(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    entity_id = _entity_id(hass, "sensor", f"{UID_BASE}_module_2_errors")
    assert er.async_get(hass).async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_no_modules_without_password(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, password="", rest=False)
    assert _entity_id(hass, "binary_sensor", f"{UID_BASE}_module_2_connected") is None
    assert _device(hass, entry, f"{UID_BASE}_module_2") is None


# --- Apex Fusion and Wi-Fi --------------------------------------------------


async def test_fusion_connected(hass: HomeAssistant, aioclient_mock) -> None:
    # status.json has the link, so no password is needed
    await _setup(hass, aioclient_mock, password="", rest=False)
    fusion = _state(hass, "binary_sensor", f"{UID_BASE}_fusion")
    assert fusion.attributes["friendly_name"] == "Apex Fusion"
    assert fusion.state == STATE_ON
    assert fusion.attributes["link_state"] == "connected"
    # the link key isn't kept
    assert "XXXXXX" not in str(fusion.attributes)


@pytest.mark.parametrize(("link_state", "label"), [(1, "not_linked"), (0, "no_communication")])
async def test_fusion_not_connected(hass: HomeAssistant, aioclient_mock, link_state, label) -> None:
    def edit(istat):
        istat["link"]["linkState"] = link_state

    await _setup(hass, aioclient_mock, password="", rest=False, status=_status_json(edit))
    fusion = _state(hass, "binary_sensor", f"{UID_BASE}_fusion")
    assert fusion.state == STATE_OFF
    assert fusion.attributes["link_state"] == label


async def test_wifi_signal(hass: HomeAssistant, aioclient_mock) -> None:
    def on_wifi(status):
        status["nstat"].update(wifiEnable=True, strength=72, quality=60)

    aioclient_mock.get(f"{HOST_URL}/rest/status", text=_rest_status(on_wifi))
    await _setup(hass, aioclient_mock)
    wifi = _state(hass, "sensor", f"{UID_BASE}_wifi_signal")
    assert float(wifi.state) == 72
    assert wifi.attributes["quality"] == 60
    assert wifi.attributes["unit_of_measurement"] == "%"


async def test_no_wifi_signal_on_ethernet(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    assert _entity_id(hass, "sensor", f"{UID_BASE}_wifi_signal") is None


# --- probe calibration -----------------------------------------------------


async def test_calibration_is_noticed(hass: HomeAssistant, aioclient_mock, monkeypatch) -> None:
    entry = await _setup(hass, aioclient_mock)

    ph = _state(hass, "sensor", f"{UID_BASE}_ph_calibrated")
    assert ph.attributes["friendly_name"] == "Apex pH Calibrated"
    # the Apex keeps no history: unknown until the next calibration
    assert ph.state == STATE_UNKNOWN
    assert _entity_id(hass, "sensor", f"{UID_BASE}_salt_calibrated")
    # temperature and Trident readings aren't calibrated this way
    assert _entity_id(hass, "sensor", f"{UID_BASE}_tmp_calibrated") is None
    assert _entity_id(hass, "sensor", f"{UID_BASE}_alkx5_calibrated") is None

    def calibrate_ph(config):
        next(i for i in config["iconf"] if i["name"] == "pH")["extra"].update(scale="1190", offset="-90")

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}/status.json", text=_status_json())
    aioclient_mock.get(f"{HOST_URL}/rest/config", text=_rest_config(calibrate_ph))
    _mock_rest(aioclient_mock)
    await _refresh_extras(hass, entry, monkeypatch)

    ph = _state(hass, "sensor", f"{UID_BASE}_ph_calibrated")
    assert parse_datetime(ph.state) is not None
    assert _state(hass, "sensor", f"{UID_BASE}_salt_calibrated").state == STATE_UNKNOWN


async def test_no_calibration_without_password(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, password="", rest=False)
    assert _entity_id(hass, "sensor", f"{UID_BASE}_ph_calibrated") is None


# --- feed durations ---------------------------------------------------------


async def test_feed_durations(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.put(f"{HOST_URL}/rest/config/misc", json=OK)
    await _setup(hass, aioclient_mock)

    feed_a = _state(hass, "number", f"{UID_BASE}_feed_a_duration")
    assert feed_a.attributes["friendly_name"] == "Apex Feed A Duration"
    assert float(feed_a.state) == 300
    assert feed_a.attributes["unit_of_measurement"] == "s"
    assert float(_state(hass, "number", f"{UID_BASE}_feed_d_duration").state) == 240

    await hass.services.async_call(
        NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: feed_a.entity_id, ATTR_VALUE: 600}, blocking=True
    )
    ((path, body),) = _puts(aioclient_mock)
    assert path == "/rest/config/misc"
    misc = json.loads(load_fixture("rest_config.json"))["misc"]
    # the rest is sent back as it was, except "reboot", which would reboot the Apex
    assert body == {k: v for k, v in misc.items() if k != "reboot"} | {"feedInterval": [600, 65535, 300, 240]}


async def test_feed_duration_limits(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    entity_id = _entity_id(hass, "number", f"{UID_BASE}_feed_a_duration")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 30}, blocking=True
        )
    assert _puts(aioclient_mock) == []


async def test_no_feed_durations_read_only(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, options={CONF_READ_ONLY: True})
    assert _entity_id(hass, "number", f"{UID_BASE}_feed_a_duration") is None


# --- variable outputs as lights and fans -----------------------------------

LEVEL_OPTIONS = {CONF_FAN_OUTPUTS: ["0"], CONF_LIGHT_OUTPUTS: ["1"]}


async def test_level_outputs_need_picking(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    assert hass.states.async_entity_ids(LIGHT_DOMAIN) == []
    assert hass.states.async_entity_ids(FAN_DOMAIN) == []


async def test_level_outputs_need_control(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, options={**LEVEL_OPTIONS, CONF_READ_ONLY: True})
    assert hass.states.async_entity_ids(LIGHT_DOMAIN) == []
    assert hass.states.async_entity_ids(FAN_DOMAIN) == []


async def test_light(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{HOST_URL}/rest/config", text=_rest_config(_set_prog("base_Var2", "Set 60\n")))
    aioclient_mock.put(f"{HOST_URL}/rest/config/oconf/base_Var2", json=OK)
    await _setup(hass, aioclient_mock, options=LEVEL_OPTIONS)

    light = _state(hass, "light", f"{UID_BASE}_output_1_light")
    assert light.attributes["friendly_name"] == "Apex VarSpd2_I2"
    assert light.state == STATE_ON
    assert light.attributes[ATTR_BRIGHTNESS] == 153

    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: light.entity_id, ATTR_BRIGHTNESS: 128}, blocking=True
    )
    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: light.entity_id}, blocking=True)
    assert [(path, body["prog"]) for path, body in _puts(aioclient_mock)] == [
        ("/rest/config/oconf/base_Var2", "Set 50\n"),
        ("/rest/config/oconf/base_Var2", "Set OFF\n"),
    ]
    # everything else is sent back as the Apex had it
    assert _puts(aioclient_mock)[0][1] == {**_oconf("base_Var2"), "ctype": "Advanced", "prog": "Set 50\n"}


async def test_light_running_a_profile_is_unknown(hass: HomeAssistant, aioclient_mock) -> None:
    # the fixture's variable outputs run profiles (Set PF2)
    await _setup(hass, aioclient_mock, options=LEVEL_OPTIONS)
    assert _state(hass, "light", f"{UID_BASE}_output_1_light").state == STATE_UNKNOWN


async def test_light_in_manual_mode_goes_back_to_auto(hass: HomeAssistant, aioclient_mock) -> None:
    def manual_off(istat):
        next(o for o in istat["outputs"] if o["did"] == "base_Var2")["status"][0] = "OFF"

    aioclient_mock.put(f"{HOST_URL}/rest/config/oconf/base_Var2", json=OK)
    aioclient_mock.put(f"{HOST_URL}/rest/status/outputs/base_Var2", json=OK)
    await _setup(hass, aioclient_mock, options=LEVEL_OPTIONS, status=_status_json(manual_off))
    light = _state(hass, "light", f"{UID_BASE}_output_1_light")
    assert light.state == STATE_OFF

    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: light.entity_id}, blocking=True)
    puts = _puts(aioclient_mock)
    assert [path for path, _ in puts] == ["/rest/config/oconf/base_Var2", "/rest/status/outputs/base_Var2"]
    # no level known: full
    assert puts[0][1]["prog"] == "Set 100\n"
    assert puts[1][1]["status"][0] == "AUTO"


async def test_fan(hass: HomeAssistant, aioclient_mock) -> None:
    def edit(istat):
        # the Apex reports the level of some outputs
        next(o for o in istat["outputs"] if o["did"] == "base_Var1")["intensity"] = 60

    aioclient_mock.put(f"{HOST_URL}/rest/config/oconf/base_Var1", json=OK)
    await _setup(hass, aioclient_mock, options=LEVEL_OPTIONS, status=_status_json(edit))

    fan = _state(hass, "fan", f"{UID_BASE}_output_0_fan")
    assert fan.attributes["friendly_name"] == "Apex VarSpd1_I1"
    assert fan.state == STATE_ON
    assert fan.attributes[ATTR_PERCENTAGE] == 60

    await hass.services.async_call(
        FAN_DOMAIN, SERVICE_SET_PERCENTAGE, {ATTR_ENTITY_ID: fan.entity_id, ATTR_PERCENTAGE: 30}, blocking=True
    )
    await hass.services.async_call(FAN_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: fan.entity_id}, blocking=True)
    # on again without a speed: where it was
    await hass.services.async_call(FAN_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: fan.entity_id}, blocking=True)
    assert [body["prog"] for _, body in _puts(aioclient_mock)] == ["Set 30\n", "Set OFF\n", "Set 60\n"]


async def test_only_variable_outputs_can_be_picked(hass: HomeAssistant, aioclient_mock) -> None:
    # output 9 is an outlet
    await _setup(hass, aioclient_mock, options={CONF_LIGHT_OUTPUTS: ["9"]})
    assert hass.states.async_entity_ids(LIGHT_DOMAIN) == []
