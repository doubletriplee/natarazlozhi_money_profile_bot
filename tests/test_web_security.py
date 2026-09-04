from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from money_profile_bot.config import Settings
from money_profile_bot.services.robokassa import RobokassaClient
from money_profile_bot.services.store import Store
from money_profile_bot.web.app import create_web_app
from money_profile_bot.web.security import SECURITY_HEADERS, HttpRateLimiter


@dataclass
class Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


def app_with_limiter(limiter: HttpRateLimiter | None = None) -> web.Application:
    return create_web_app(
        Settings(_env_file=None),
        cast(Store, AsyncMock()),
        cast(RobokassaClient, AsyncMock()),
        None,
        rate_limiter=limiter,
    )


@pytest.mark.asyncio
async def test_security_headers_cover_pages_and_http_errors() -> None:
    async with TestClient(TestServer(app_with_limiter())) as client:
        home = await client.get("/")
        not_found = await client.get("/missing")

    for response in (home, not_found):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value


@pytest.mark.asyncio
async def test_http_rate_limit_uses_caddys_last_forwarded_client_address() -> None:
    limiter = HttpRateLimiter(capacity=2, refill_per_second=0.01, clock=Clock())
    headers = {"X-Forwarded-For": "spoofed, 203.0.113.10"}
    async with TestClient(TestServer(app_with_limiter(limiter))) as client:
        first = await client.get("/", headers=headers)
        second = await client.get("/", headers=headers)
        blocked = await client.get("/", headers=headers)
        another_client = await client.get(
            "/",
            headers={"X-Forwarded-For": "spoofed, 203.0.113.11"},
        )

    assert first.status == 200
    assert second.status == 200
    assert blocked.status == 429
    assert blocked.headers["Retry-After"] == "10"
    assert blocked.headers["X-Frame-Options"] == "DENY"
    assert another_client.status == 200
