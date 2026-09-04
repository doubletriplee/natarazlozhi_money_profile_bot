from __future__ import annotations

import asyncio

import pytest

from money_profile_bot.bot.calculation_gate import CalculationAdmission, CalculationGate


@pytest.mark.asyncio
async def test_gate_deduplicates_users_and_bounds_queue() -> None:
    gate = CalculationGate(max_parallel=1, max_queued=2)
    assert await gate.acquire(10001) is CalculationAdmission.ACQUIRED
    assert await gate.acquire(10001) is CalculationAdmission.DUPLICATE

    queued = asyncio.create_task(gate.acquire(20002))
    await asyncio.sleep(0)
    assert not queued.done()
    assert await gate.acquire(30003) is CalculationAdmission.BUSY

    await gate.release(10001)
    assert await queued is CalculationAdmission.ACQUIRED
    await gate.release(20002)


@pytest.mark.asyncio
async def test_cancelled_waiter_releases_its_queue_slot() -> None:
    gate = CalculationGate(max_parallel=1, max_queued=2)
    assert await gate.acquire(10001) is CalculationAdmission.ACQUIRED
    queued = asyncio.create_task(gate.acquire(20002))
    await asyncio.sleep(0)

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    replacement = asyncio.create_task(gate.acquire(30003))
    await asyncio.sleep(0)
    assert not replacement.done()
    await gate.release(10001)
    assert await replacement is CalculationAdmission.ACQUIRED
    await gate.release(30003)
