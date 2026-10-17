# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json

import pytest

from custom_components.apex.coordinator import (
    _format_apex_time,
    _parse_status_json,
    _parse_status_xml,
)

from .conftest import load_fixture


def _status_json(inputs: list[dict]) -> str:
    return json.dumps({"istat": {"inputs": inputs, "outputs": []}})


def test_json_matches_xml():
    """Both endpoints of the same Apex parse to the same data (status.json is a superset)."""
    j = _parse_status_json(load_fixture("status.json"))
    x = _parse_status_xml(load_fixture("status.xml"))

    assert j["source"] == "json"
    assert x["source"] == "xml"
    assert j["meta"] == x["meta"]
    assert j["power"] == x["power"]

    assert j["probes"].keys() == x["probes"].keys()
    for name, probe in x["probes"].items():
        assert j["probes"][name]["type"] == probe["type"]
        assert j["probes"][name]["value"] == probe["value"]

    # status.xml also lists a virtual email alarm output that status.json leaves out
    # status.xml has only the state of each output; status.json has its type and full status too
    shared = ("name", "output_id", "state", "device_id")
    xml_outlets = {o["output_id"]: {k: o[k] for k in shared} for o in x["outlets"] if o["name"] != "Email2Alm_I9"}
    assert {o["output_id"]: {k: o[k] for k in shared} for o in j["outlets"]} == xml_outlets


def test_json_meta_and_power():
    data = _parse_status_json(load_fixture("status.json"))

    assert data["meta"] == {
        "software": "5.15_6B26",
        "hardware": "1.0",
        "hostname": "apex",
        "serial": "AC5:12345",
        "timezone": "-7.00",
        "date": "10/17/2026 10:48:58",
    }
    assert data["power"] == {"failed": "10/04/2026 15:04:52", "restored": "10/04/2026 15:04:57"}


def test_json_probes_and_outlets():
    data = _parse_status_json(load_fixture("status.json"))

    assert data["probes"]["Tmp"] == {"name": "Tmp", "value": 78.6, "value_raw": "78.6", "type": "Temp"}
    assert data["probes"]["Cax5"]["value"] == 443.0
    # digital inputs are not probes
    assert not any(p["type"] == "digital" for p in data["probes"].values())

    outlets = {o["output_id"]: o for o in data["outlets"]}
    assert outlets["9"] == {
        "name": "DOS",
        "output_id": "9",
        "state": "ON",
        "device_id": "2_2",
        "type": "outlet",
        "gid": None,
        "status": ["ON", None, "OK", None],
        "intensity": None,
    }
    assert outlets["0"]["state"] == "PF1"


def test_json_digital_inputs():
    inputs = _parse_status_json(load_fixture("status.json"))["inputs"]

    assert len(inputs) == 26
    assert inputs["base_I1"] == {"did": "base_I1", "name": "Sw1", "value": 0, "state": "OPEN"}
    assert inputs["3_I1"] == {"did": "3_I1", "name": "Swx3_1", "value": 200, "state": "CLOSED"}


def test_issue_1_sample():
    """Inputs from the payload posted in issue #1."""
    inputs = _parse_status_json(
        _status_json(
            [
                {"did": "6_I1", "name": "OV_LOW", "type": "digital", "value": 200},
                {"did": "6_I2", "name": "OV_HIG", "type": "digital", "value": 0},
                {"did": "8_I4", "name": "ZV_TOP", "type": "digital", "value": 0},
            ]
        )
    )["inputs"]

    assert {did: i["state"] for did, i in inputs.items()} == {
        "6_I1": "CLOSED",
        "6_I2": "OPEN",
        "8_I4": "OPEN",
    }


def test_unexpected_digital_value_is_unknown():
    inputs = _parse_status_json(_status_json([{"did": "base_I1", "name": "Sw1", "type": "digital", "value": 7}]))["inputs"]
    assert inputs["base_I1"]["state"] is None


def test_digital_input_without_did_is_skipped():
    inputs = _parse_status_json(_status_json([{"name": "Sw1", "type": "digital", "value": 0}]))["inputs"]
    assert inputs == {}


@pytest.mark.parametrize("text", ["not json", "[]", "{}", '{"istat": []}'])
def test_bad_status_json_raises(text):
    with pytest.raises(ValueError):
        _parse_status_json(text)


@pytest.mark.parametrize(
    ("epoch", "tz_hours", "expected"),
    [
        (1791151492, -7.0, "10/04/2026 15:04:52"),
        (1791151492, 0.0, "10/04/2026 22:04:52"),
        (1791151492, 5.5, "10/05/2026 03:34:52"),
        (0, -7.0, None),
        (None, -7.0, None),
        ("1791151492", -7.0, None),
    ],
)
def test_format_apex_time(epoch, tz_hours, expected):
    assert _format_apex_time(epoch, tz_hours) == expected


def test_json_feed_state():
    assert _parse_status_json(load_fixture("status.json"))["feed"] == {"name": 0, "active": 0}
    assert _parse_status_xml(load_fixture("status.xml"))["feed"] is None
    running = json.dumps({"istat": {"feed": {"name": 1, "active": 299}}})
    assert _parse_status_json(running)["feed"] == {"name": 1, "active": 299}
    assert _parse_status_json('{"istat": {}}')["feed"] is None


def test_json_probes_with_the_same_name():
    """Two inputs with one name (e.g. two "Leak" probes) both become probes."""
    data = _parse_status_json(
        _status_json(
            [
                {"did": "1_P1", "type": "in", "name": "Leak", "value": 1},
                {"did": "1_P2", "type": "in", "name": "Leak", "value": 2},
            ]
        )
    )
    assert data["probes"]["Leak"]["value"] == 1
    assert data["probes"]["Leak_1_P2"] == {"name": "Leak", "value": 2.0, "value_raw": "2", "type": "in"}


def test_labels_for_per_outlet_power():
    from custom_components.apex.parsing import build_labels

    probes = {
        "Tmp": {"name": "Tmp", "type": "Temp"},
        "Tmp2": {"name": "Tmp2", "type": "Temp"},
        "Return_2_1W": {"name": "Return_2_1W", "type": "pwr"},
        "Skimmer_2_2W": {"name": "Skimmer_2_2W", "type": "pwr"},
        "ORP": {"name": "ORP", "type": "ORP"},
    }
    assert build_labels(probes) == {
        "Tmp": "Temp",
        "Tmp2": "Temp 2",
        "Return_2_1W": "Return_2_1W",
        "Skimmer_2_2W": "Skimmer_2_2W",
        "ORP": "ORP",
    }


@pytest.mark.parametrize(
    ("prog", "expected"),
    [
        ("Set 75\n", 75),
        ("Set 12.5", 12.5),
        ("Set PF1\n", "PF1"),
        ("Fallback OFF\nSet 75\n", None),
        ("If Tmp > 80 Then OFF", None),
        (None, None),
    ],
)
def test_program_setting(prog, expected):
    from custom_components.apex.sensor import program_setting

    assert program_setting(prog) == expected


def test_json_probes_with_the_same_name_and_no_device_id():
    data = _parse_status_json(
        _status_json(
            [
                {"type": "in", "name": "Leak", "value": 1},
                {"did": "1_P1", "type": "Temp", "name": "Tmp", "value": 78},
                {"did": "1_P2", "type": "Temp", "name": "Tmp2", "value": 79},
                {"type": "in", "name": "Leak", "value": 2},
            ]
        )
    )
    # numbered among the probes of that name, so other probes don't change the key
    assert data["probes"]["Leak_2"]["value"] == 2.0
