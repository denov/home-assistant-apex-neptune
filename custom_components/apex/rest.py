# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client for the Apex AOS REST API (/rest/...), which needs a login session.

Live readings come from status.json in the coordinator; this is for control and for what
only the REST API has (module status such as Trident levels, and the configuration).
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

# status.json feed.name: 0 = no feed, 1-4 = feed A-D
FEED_LETTERS: dict[int, str] = {1: "A", 2: "B", 3: "C", 4: "D"}

# what the Apex UI sends as status[0] to change an output's mode
OUTPUT_MODES = ("AUTO", "ON", "OFF")

SESSION_COOKIE = "connect.sid"

# DOS/DQD pump speeds (mL/min) by the speed index in bits 0-3 of a dose profile's mode
DOS_PUMP_SPEEDS = (250, 125, 60, 25, 12, 7)
# bit 4 of the mode: forward (the only direction the Apex can calibrate)
DOS_FORWARD = 16
# run the pump at no more than half its speed; Neptune's own UI keeps to a third
DOS_SAFETY_MARGIN = 2
DOS_MAX_RATE = DOS_PUMP_SPEEDS[0] / DOS_SAFETY_MARGIN
DOS_MIN_RATE = 0.1


class ApexRestError(Exception):
    """The Apex rejected or failed a REST request."""


class ApexAuthError(ApexRestError):
    """Login failed: wrong username or password."""


class ApexNoRestError(ApexRestError):
    """The Apex has no REST API (e.g. an Apex Classic), so it can't be controlled from here."""


def _is_basic_challenge(resp: aiohttp.ClientResponse) -> bool:
    """An Apex Classic has no /rest API and asks for HTTP Basic auth on its pages instead."""
    return resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def dos_profile_mode(rate: float) -> int:
    """Profile mode for a dose rate (mL/min): the slowest pump speed with the safety margin, forward."""
    target = rate * DOS_SAFETY_MARGIN
    index = len(DOS_PUMP_SPEEDS) - 1
    while DOS_PUMP_SPEEDS[index] < target:
        index -= 1
    return index + DOS_FORWARD


class ApexRestClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        host_url: str,
        username: str,
        password: str,
        timeout_seconds: int = 10,
    ) -> None:
        self._session = session
        self._host_url = host_url.rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout_seconds
        self._sid: str | None = None
        self._lock = asyncio.Lock()

    async def async_login(self) -> None:
        async with asyncio.timeout(self._timeout):
            async with self._session.post(
                f"{self._host_url}/rest/login",
                json={"login": self._username, "password": self._password, "remember_me": False},
            ) as resp:
                if resp.status in (401, 403):
                    if _is_basic_challenge(resp):
                        raise ApexNoRestError("This Apex has no REST API (Apex Classic), so it can only be monitored")
                    raise ApexAuthError("Apex login failed: check the username and password")
                resp.raise_for_status()
                body = await resp.json(content_type=None)

        sid = body.get(SESSION_COOKIE) if isinstance(body, dict) else None
        if not sid:
            raise ApexRestError("Apex login response had no session")
        self._sid = sid

    async def _async_request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        # one request at a time, so a re-login can't race another request
        async with self._lock:
            if self._sid is None:
                await self.async_login()

            status, result = await self._async_request_once(method, path, body)
            if status in (401, 403):
                # session expired (e.g. the Apex rebooted): log in again once
                await self.async_login()
                status, result = await self._async_request_once(method, path, body)
                if status in (401, 403):
                    raise ApexAuthError("Apex rejected the request after logging in again")

        if result.get("errorCode"):
            raise ApexRestError(f"Apex error {result['errorCode']}: {result.get('errorMessage') or 'unknown'}")
        return result

    async def _async_request_once(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        async with asyncio.timeout(self._timeout):
            async with self._session.request(
                method,
                f"{self._host_url}{path}",
                json=body,
                headers={"Cookie": f"{SESSION_COOKIE}={self._sid}"},
            ) as resp:
                if resp.status in (401, 403):
                    return resp.status, {}
                resp.raise_for_status()
                result = await resp.json(content_type=None)
        return resp.status, (result if isinstance(result, dict) else {})

    async def _async_put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._async_request("PUT", path, body)

    async def async_get_status(self) -> dict[str, Any]:
        """/rest/status: what status.json has, plus modules (e.g. Trident levels)."""
        return await self._async_request("GET", "/rest/status")

    async def async_get_config(self) -> dict[str, Any]:
        """/rest/config: output programs (oconf), profiles (pconf), modules (mconf), network (nconf), ..."""
        return await self._async_request("GET", "/rest/config")

    async def async_start_feed(self, feed: int) -> None:
        """Start feed cycle 1-4 (A-D)."""
        # name 0 with active 1 isn't "no feed": the Apex starts a bogus ~17 hour cycle
        if feed not in FEED_LETTERS:
            raise ValueError(f"feed must be 1-4 (A-D), not {feed!r}")
        await self._async_put(f"/rest/status/feed/{feed}", {"name": feed, "active": 1})

    async def async_cancel_feed(self) -> None:
        await self._async_put("/rest/status/feed/0", {"name": 0, "active": 0})

    async def async_set_output(self, did: str, mode: str) -> None:
        """Set an output to AUTO, ON or OFF, as the Apex dashboard does."""
        if mode not in OUTPUT_MODES:
            raise ValueError(f"mode must be one of {OUTPUT_MODES}, not {mode!r}")
        # the Apex accepts "outlet" for any output type
        await self._async_put(
            f"/rest/status/outputs/{did}",
            {"did": did, "status": [mode, "", "OK", ""], "type": "outlet"},
        )

    async def _async_output_config(self, did: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
        config = config if config is not None else await self.async_get_config()
        for output in config.get("oconf") or []:
            if isinstance(output, dict) and output.get("did") == did:
                return output
        raise ApexRestError(f"Apex has no output {did}")

    async def async_set_program(self, did: str, program: str, config: dict[str, Any] | None = None) -> None:
        """Replace an output's program (e.g. "Set 75"), making it an Advanced output.

        The output's current configuration is sent back unchanged apart from the program,
        since the Apex doesn't validate what it's given.
        """
        program = program.strip()
        if not program:
            raise ValueError("program can't be empty")
        output = dict(await self._async_output_config(did, config))
        output["ctype"] = "Advanced"
        output["prog"] = program.replace("\r\n", "\n") + "\n"
        await self._async_put(f"/rest/config/oconf/{did}", output)

    async def async_set_dos_rate(self, did: str, profile_id: int, rate: float) -> None:
        """Run a DOS/DQD pump continuously at rate mL/min, using profile profile_id for it.

        The profile is overwritten and renamed Dose_<did>. A rate below 0.1 mL/min turns the pump off.
        """
        if rate > DOS_MAX_RATE:
            raise ValueError(f"rate {rate} mL/min is above the {DOS_MAX_RATE:g} mL/min limit")

        config = await self.async_get_config()
        profile = next(
            (p for p in config.get("pconf") or [] if isinstance(p, dict) and str(p.get("ID")) == str(profile_id)),
            None,
        )
        if profile is None:
            raise ApexRestError(f"Apex has no profile {profile_id}")
        await self._async_output_config(did, config)

        # off first, so a new profile starts now rather than when the current dose period ends
        await self.async_set_program(did, "Set OFF", config)
        rate = int(rate * 10) / 10.0
        if rate < DOS_MIN_RATE:
            return

        name = f"Dose_{did}"
        profile = {
            **profile,
            "name": name,
            "type": "dose",
            # one dose of rate mL every 60 seconds, repeating
            "data": {"mode": dos_profile_mode(rate), "amount": rate, "time": 60, "count": 255},
        }
        await self._async_put(f"/rest/config/pconf/{profile_id}", profile)
        await self.async_set_program(did, f"Set {name}", config)

    async def async_update_firmware(self) -> None:
        """Have the Apex download and install the latest AOS. It goes offline while it does."""
        config = await self.async_get_config()
        nconf = config.get("nconf")
        if not isinstance(nconf, dict):
            raise ApexRestError("Apex config has no network settings")
        await self._async_put("/rest/config/nconf", {**nconf, "updateFirmware": True})
