from __future__ import annotations

from dataclasses import dataclass

from money_profile_bot.bot.rate_limit import TelegramRateLimiter


@dataclass
class Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


def test_rate_limiter_allows_burst_then_refills() -> None:
    clock = Clock()
    limiter = TelegramRateLimiter(
        capacity=2,
        refill_per_second=1,
        notice_interval=10,
        clock=clock,
    )

    assert limiter.check(10001) == (True, False)
    assert limiter.check(10001) == (True, False)
    assert limiter.check(10001) == (False, True)
    assert limiter.check(10001) == (False, False)

    clock.value = 1.0
    assert limiter.check(10001) == (True, False)


def test_rate_limiter_is_independent_per_user_and_repeats_notice_later() -> None:
    clock = Clock()
    limiter = TelegramRateLimiter(
        capacity=1,
        refill_per_second=0.01,
        notice_interval=10,
        clock=clock,
    )

    assert limiter.check(10001) == (True, False)
    assert limiter.check(10001) == (False, True)
    assert limiter.check(20002) == (True, False)

    clock.value = 10.0
    assert limiter.check(10001) == (False, True)
