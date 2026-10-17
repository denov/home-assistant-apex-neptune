# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client for the Apex AOS REST API (/rest/...), which needs a login session.

Reads go through status.json in the coordinator; this is only for control.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

# status.json feed.name: 0 = no feed, 1-4 = feed A-D
FEED_LETTERS: dict[int, str] = {1: "A", 2: "B", 3: "C", 4: "D"}

SESSION_COOKIE = "connect.sid"


class ApexRestError(Exception):
    """The Apex rejected or failed a REST request."""


class ApexAuthError(ApexRestError):
    """Login failed: wrong username or password."""


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
        self._username = username or "admin"
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
                    raise ApexAuthError("Apex login failed: check the username and password")
                resp.raise_for_status()
                body = await resp.json(content_type=None)

        sid = body.get(SESSION_COOKIE) if isinstance(body, dict) else None
        if not sid:
            raise ApexRestError("Apex login response had no session")
        self._sid = sid

    async def _async_put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        # one request at a time, so a re-login can't race another command
        async with self._lock:
            if self._sid is None:
                await self.async_login()

            status, result = await self._async_put_once(path, body)
            if status in (401, 403):
                # session expired (e.g. the Apex rebooted): log in again once
                await self.async_login()
                status, result = await self._async_put_once(path, body)
                if status in (401, 403):
                    raise ApexAuthError("Apex rejected the request after logging in again")

        if result.get("errorCode"):
            raise ApexRestError(f"Apex error {result['errorCode']}: {result.get('errorMessage') or 'unknown'}")
        return result

    async def _async_put_once(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with asyncio.timeout(self._timeout):
            async with self._session.put(
                f"{self._host_url}{path}",
                json=body,
                headers={"Cookie": f"{SESSION_COOKIE}={self._sid}"},
            ) as resp:
                if resp.status in (401, 403):
                    return resp.status, {}
                resp.raise_for_status()
                result = await resp.json(content_type=None)
        return resp.status, (result if isinstance(result, dict) else {})

    async def async_start_feed(self, feed: int) -> None:
        """Start feed cycle 1-4 (A-D)."""
        # name 0 with active 1 isn't "no feed": the Apex starts a bogus ~17 hour cycle
        if feed not in FEED_LETTERS:
            raise ValueError(f"feed must be 1-4 (A-D), not {feed!r}")
        await self._async_put(f"/rest/status/feed/{feed}", {"name": feed, "active": 1})

    async def async_cancel_feed(self) -> None:
        await self._async_put("/rest/status/feed/0", {"name": 0, "active": 0})
