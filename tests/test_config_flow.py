# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import aiohttp
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apex.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_USERNAME, DOMAIN

from .conftest import APEX_IP, BASE_URL, load_fixture

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
