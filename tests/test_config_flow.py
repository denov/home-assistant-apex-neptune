# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apex.const import (
    CONF_IP_ADDRESS,
    CONF_LINE_VOLTAGE,
    CONF_PASSWORD,
    CONF_READ_ONLY,
    CONF_USERNAME,
    DOMAIN,
)

from .conftest import APEX_IP, BASE_URL, HOST_URL, UID_BASE, load_fixture

USER_INPUT = {CONF_IP_ADDRESS: APEX_IP, CONF_USERNAME: "admin", CONF_PASSWORD: ""}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_create_entry(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))

    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert "Serial: AC5:12345" in result["description_placeholders"]["info"]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Apex @ {APEX_IP}"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == UID_BASE


async def test_same_apex_at_a_new_address(hass: HomeAssistant, aioclient_mock) -> None:
    """Found by its serial: the entry gets the new address instead of a second entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={**USER_INPUT, CONF_IP_ADDRESS: "9.9.9.9"}, unique_id=UID_BASE)
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_IP_ADDRESS] == APEX_IP


async def test_create_entry_xml_only_apex(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", status=404)
    aioclient_mock.get(f"{BASE_URL}/status.xml", text=load_fixture("status.xml"))

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["step_id"] == "confirm"
    assert "Serial: AC5:12345" in result["description_placeholders"]["info"]


async def test_cannot_connect(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", exc=aiohttp.ClientError("unreachable"))

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_already_configured(hass: HomeAssistant, aioclient_mock) -> None:
    MockConfigEntry(domain=DOMAIN, data={CONF_IP_ADDRESS: APEX_IP}, unique_id=APEX_IP).add_to_hass(hass)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_wrong_password(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))
    aioclient_mock.post(f"http://{APEX_IP}/rest/login", status=401)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_PASSWORD: "wrong"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_password_checked_against_rest_login(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))
    aioclient_mock.post(f"http://{APEX_IP}/rest/login", json={"connect.sid": "sid-1"})

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_PASSWORD: "secret"})
    assert result["step_id"] == "confirm"


async def test_firmware_without_rest_login_can_still_be_added(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/status.json", status=404)
    aioclient_mock.get(f"{BASE_URL}/status.xml", text=load_fixture("status.xml"))
    aioclient_mock.post(f"http://{APEX_IP}/rest/login", status=404)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_PASSWORD: "secret"})
    assert result["step_id"] == "confirm"


def test_normalize_host():
    from custom_components.apex.config_flow import normalize_host

    assert normalize_host(" apex.local ") == "apex.local"
    assert normalize_host("http://apex.local/") == "apex.local"
    assert normalize_host("HTTP://192.168.1.50/cgi-bin/status.json") == "192.168.1.50"


# --- options ----------------------------------------------------------------


def _options_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, CONF_PASSWORD: "secret"},
        # connection settings used to be saved in the options too
        options={CONF_LINE_VOLTAGE: 230, CONF_USERNAME: "admin"},
        unique_id=UID_BASE,
    )
    entry.add_to_hass(hass)
    return entry


async def test_options_never_show_the_password(hass: HomeAssistant) -> None:
    entry = _options_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    password = next(k for k in schema if k == CONF_PASSWORD)
    assert password.default is vol.UNDEFINED
    assert "secret" not in str(result)


async def test_options_keep_the_password_when_left_blank(hass: HomeAssistant, aioclient_mock) -> None:
    entry = _options_entry(hass)
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))
    aioclient_mock.post(f"{HOST_URL}/rest/login", json={"connect.sid": "sid-1"})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IP_ADDRESS: f"http://{APEX_IP}/", CONF_USERNAME: "admin", CONF_READ_ONLY: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    login = next(c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/rest/login"))
    assert login[2]["password"] == "secret"
    assert entry.data == {CONF_IP_ADDRESS: APEX_IP, CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}
    assert entry.options[CONF_READ_ONLY] is True
    assert CONF_PASSWORD not in entry.options and CONF_USERNAME not in entry.options


async def test_options_check_the_connection(hass: HomeAssistant, aioclient_mock) -> None:
    entry = _options_entry(hass)
    aioclient_mock.get("http://5.6.7.8/cgi-bin/status.json", exc=aiohttp.ClientError("unreachable"))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IP_ADDRESS: "5.6.7.8", CONF_USERNAME: "admin"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_IP_ADDRESS] == APEX_IP


async def test_options_check_a_new_password(hass: HomeAssistant, aioclient_mock) -> None:
    entry = _options_entry(hass)
    aioclient_mock.get(f"{BASE_URL}/status.json", text=load_fixture("status.json"))
    aioclient_mock.post(f"{HOST_URL}/rest/login", status=401)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IP_ADDRESS: APEX_IP, CONF_USERNAME: "admin", CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PASSWORD] == "secret"
