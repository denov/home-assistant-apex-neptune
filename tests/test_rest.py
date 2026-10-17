# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.apex.rest import ApexAuthError, ApexRestClient, ApexRestError

from .conftest import HOST_URL

LOGIN_URL = f"{HOST_URL}/rest/login"


def _client(hass: HomeAssistant) -> ApexRestClient:
    return ApexRestClient(async_get_clientsession(hass), HOST_URL, "admin", "secret")


def _calls(aioclient_mock, method: str, path: str) -> list:
    return [c for c in aioclient_mock.mock_calls if c[0] == method and str(c[1]) == f"{HOST_URL}{path}"]


async def test_start_feed_logs_in_and_sends_session(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, json={"connect.sid": "sid-1"})
    aioclient_mock.put(f"{HOST_URL}/rest/status/feed/1", json={"name": 1, "active": 1, "errorCode": 0, "errorMessage": ""})

    await _client(hass).async_start_feed(1)

    (login,) = _calls(aioclient_mock, "POST", "/rest/login")
    assert login[2] == {"login": "admin", "password": "secret", "remember_me": False}
    (put,) = _calls(aioclient_mock, "PUT", "/rest/status/feed/1")
    assert put[2] == {"name": 1, "active": 1}
    assert put[3]["Cookie"] == "connect.sid=sid-1"


async def test_session_is_reused(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, json={"connect.sid": "sid-1"})
    aioclient_mock.put(f"{HOST_URL}/rest/status/feed/1", json={"errorCode": 0})
    aioclient_mock.put(f"{HOST_URL}/rest/status/feed/0", json={"errorCode": 0})
    client = _client(hass)

    await client.async_start_feed(1)
    await client.async_cancel_feed()

    assert len(_calls(aioclient_mock, "POST", "/rest/login")) == 1


async def test_cancel_feed(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, json={"connect.sid": "sid-1"})
    aioclient_mock.put(f"{HOST_URL}/rest/status/feed/0", json={"name": 0, "active": 0, "errorCode": 0})

    await _client(hass).async_cancel_feed()

    (put,) = _calls(aioclient_mock, "PUT", "/rest/status/feed/0")
    assert put[2] == {"name": 0, "active": 0}


@pytest.mark.parametrize("feed", [0, 5, -1, None, "A"])
async def test_start_feed_rejects_bad_feed(hass: HomeAssistant, aioclient_mock, feed) -> None:
    """name 0 + active 1 makes the Apex start a bogus ~17 hour cycle; never send it."""
    with pytest.raises(ValueError):
        await _client(hass).async_start_feed(feed)
    assert aioclient_mock.call_count == 0


async def test_logs_in_again_when_session_expires(hass: HomeAssistant, aioclient_mock) -> None:
    sids = iter(["sid-1", "sid-2"])

    async def login(method, url, data):
        return AiohttpClientMockResponse(method, url, json={"connect.sid": next(sids)})

    async def feed(method, url, data):
        # the first session has expired (e.g. the Apex rebooted)
        status = 401 if len(_calls(aioclient_mock, "PUT", "/rest/status/feed/2")) == 1 else 200
        return AiohttpClientMockResponse(method, url, status=status, json={"errorCode": 0})

    aioclient_mock.post(LOGIN_URL, side_effect=login)
    aioclient_mock.put(f"{HOST_URL}/rest/status/feed/2", side_effect=feed)

    await _client(hass).async_start_feed(2)

    puts = _calls(aioclient_mock, "PUT", "/rest/status/feed/2")
    assert [p[3]["Cookie"] for p in puts] == ["connect.sid=sid-1", "connect.sid=sid-2"]
    assert len(_calls(aioclient_mock, "POST", "/rest/login")) == 2


async def test_wrong_password(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, status=401, text="<HTML>401 Unauthorized</HTML>")

    with pytest.raises(ApexAuthError):
        await _client(hass).async_start_feed(1)
    assert _calls(aioclient_mock, "PUT", "/rest/status/feed/1") == []


async def test_rejected_after_fresh_login(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, json={"connect.sid": "sid-1"})
    aioclient_mock.put(f"{HOST_URL}/rest/status/feed/1", status=403)

    with pytest.raises(ApexAuthError):
        await _client(hass).async_start_feed(1)
    assert len(_calls(aioclient_mock, "PUT", "/rest/status/feed/1")) == 2


async def test_apex_error_code(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, json={"connect.sid": "sid-1"})
    aioclient_mock.put(f"{HOST_URL}/rest/status/feed/1", json={"errorCode": 3, "errorMessage": "Busy"})

    with pytest.raises(ApexRestError, match="Busy"):
        await _client(hass).async_start_feed(1)


async def test_login_without_session(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, json={})

    with pytest.raises(ApexRestError):
        await _client(hass).async_login()


async def test_get_logs_in_again_when_the_session_expires(hass: HomeAssistant, aioclient_mock) -> None:
    """After the Apex reboots, reads must keep working (upstream issue: lost connection until reload)."""
    aioclient_mock.post(LOGIN_URL, json={"connect.sid": "sid-1"})
    responses = iter([{"status": 401}, {"json": {"modules": []}}])

    async def status(method, url, data):
        return AiohttpClientMockResponse(method, url, **next(responses))

    aioclient_mock.get(f"{HOST_URL}/rest/status", side_effect=status)
    client = _client(hass)
    client._sid = "expired"

    assert await client.async_get_status() == {"modules": []}
    assert len(_calls(aioclient_mock, "POST", "/rest/login")) == 1


@pytest.mark.parametrize(("rate", "mode"), [(1.2, 21), (3.5, 21), (5, 20), (30, 18), (125, 16)])
def test_dos_profile_mode(rate, mode):
    from custom_components.apex.rest import dos_profile_mode

    assert dos_profile_mode(rate) == mode


async def test_set_output_rejects_unknown_mode(hass: HomeAssistant) -> None:
    with pytest.raises(ValueError):
        await _client(hass).async_set_output("2_2", "TOGGLE")
