from __future__ import annotations

import asyncio
from enum import StrEnum

MAX_PARALLEL_CALCULATIONS = 2
MAX_QUEUED_CALCULATIONS = 16


class CalculationAdmission(StrEnum):
    ACQUIRED = "acquired"
    DUPLICATE = "duplicate"
    BUSY = "busy"


class CalculationGate:
    def __init__(
        self,
        *,
        max_parallel: int = MAX_PARALLEL_CALCULATIONS,
        max_queued: int = MAX_QUEUED_CALCULATIONS,
    ) -> None:
        if max_parallel < 1 or max_queued < max_parallel:
            raise ValueError("invalid calculation gate limits")
        self._active_users: set[int] = set()
        self._claim_lock = asyncio.Lock()
        self._parallel = asyncio.Semaphore(max_parallel)
        self._max_queued = max_queued

    async def acquire(self, telegram_id: int) -> CalculationAdmission:
        async with self._claim_lock:
            if telegram_id in self._active_users:
                return CalculationAdmission.DUPLICATE
            if len(self._active_users) >= self._max_queued:
                return CalculationAdmission.BUSY
            self._active_users.add(telegram_id)
        try:
            await self._parallel.acquire()
        except asyncio.CancelledError:
            async with self._claim_lock:
                self._active_users.discard(telegram_id)
            raise
        return CalculationAdmission.ACQUIRED

    async def release(self, telegram_id: int) -> None:
        async with self._claim_lock:
            was_active = telegram_id in self._active_users
            self._active_users.discard(telegram_id)
        if was_active:
            self._parallel.release()
