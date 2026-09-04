from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web
from aiohttp.typedefs import Middleware

HTTP_BURST_CAPACITY = 60
HTTP_REFILL_PER_SECOND = 1.0
STALE_CLIENT_SECONDS = 300.0
CLEANUP_EVERY_CHECKS = 1024

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'; object-src 'none'; script-src 'none'; upgrade-insecure-requests"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class HttpRateLimiter:
    def __init__(
        self,
        *,
        capacity: int = HTTP_BURST_CAPACITY,
        refill_per_second: float = HTTP_REFILL_PER_SECOND,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("invalid HTTP rate limits")
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._checks = 0

    def allow(self, client_key: str) -> bool:
        now = self._clock()
        bucket = self._buckets.get(client_key)
        if bucket is None:
            bucket = _Bucket(float(self._capacity), now)
            self._buckets[client_key] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(
                float(self._capacity),
                bucket.tokens + elapsed * self._refill_per_second,
            )
            bucket.updated_at = now

        self._checks += 1
        if self._checks % CLEANUP_EVERY_CHECKS == 0:
            cutoff = now - STALE_CLIENT_SECONDS
            self._buckets = {
                key: value for key, value in self._buckets.items() if value.updated_at >= cutoff
            }

        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True


def _client_key(request: web.Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        forwarded_client = forwarded_for.rsplit(",", 1)[-1].strip()
        if forwarded_client:
            return forwarded_client[:64]
    return (request.remote or "unknown")[:64]


@web.middleware
async def security_headers_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exception:
        for name, value in SECURITY_HEADERS.items():
            exception.headers[name] = value
        raise
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def rate_limit_middleware(limiter: HttpRateLimiter) -> Middleware:
    @web.middleware
    async def middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if not limiter.allow(_client_key(request)):
            return web.Response(
                status=429,
                text="Слишком много запросов. Попробуйте позже.",
                headers={"Retry-After": "10"},
            )
        return await handler(request)

    return middleware
