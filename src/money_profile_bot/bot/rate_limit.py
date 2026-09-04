from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

TELEGRAM_BURST_CAPACITY = 20
TELEGRAM_REFILL_PER_SECOND = 2.0
RATE_LIMIT_NOTICE_INTERVAL = 10.0
STALE_BUCKET_SECONDS = 300.0
CLEANUP_EVERY_CHECKS = 1024


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    last_notice_at: float


class TelegramRateLimiter:
    def __init__(
        self,
        *,
        capacity: int = TELEGRAM_BURST_CAPACITY,
        refill_per_second: float = TELEGRAM_REFILL_PER_SECOND,
        notice_interval: float = RATE_LIMIT_NOTICE_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0 or notice_interval <= 0:
            raise ValueError("invalid Telegram rate limits")
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._notice_interval = notice_interval
        self._clock = clock
        self._buckets: dict[int, _Bucket] = {}
        self._checks = 0

    def check(self, telegram_id: int) -> tuple[bool, bool]:
        now = self._clock()
        bucket = self._buckets.get(telegram_id)
        if bucket is None:
            bucket = _Bucket(float(self._capacity), now, float("-inf"))
            self._buckets[telegram_id] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(
                float(self._capacity),
                bucket.tokens + elapsed * self._refill_per_second,
            )
            bucket.updated_at = now

        self._checks += 1
        if self._checks % CLEANUP_EVERY_CHECKS == 0:
            cutoff = now - STALE_BUCKET_SECONDS
            self._buckets = {
                user_id: value
                for user_id, value in self._buckets.items()
                if value.updated_at >= cutoff
            }

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True, False

        should_notify = now - bucket.last_notice_at >= self._notice_interval
        if should_notify:
            bucket.last_notice_at = now
        return False, should_notify


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, limiter: TelegramRateLimiter | None = None) -> None:
        self.limiter = limiter or TelegramRateLimiter()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        allowed, should_notify = self.limiter.check(user.id)
        if allowed:
            return await handler(event, data)
        if should_notify:
            with suppress(TelegramAPIError):
                if isinstance(event, CallbackQuery):
                    await event.answer(
                        "Слишком много нажатий. Подожди несколько секунд.",
                        show_alert=True,
                    )
                elif isinstance(event, Message):
                    await event.answer("Слишком много сообщений. Подожди несколько секунд.")
        return None
