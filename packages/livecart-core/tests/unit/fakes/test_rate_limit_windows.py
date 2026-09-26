"""What ``InMemoryRateLimiter`` does beyond the scenarios of tests/contract: time and failures."""

from datetime import timedelta

import pytest

from livecart_core.application.dto import RateLimitKey, WindowCount
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.testing.fakes import FakeClock, InMemoryRateLimiter

KEY = RateLimitKey("buyer_lookup", "198.51.100.23")


async def test_a_window_ends_its_length_after_its_first_hit() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock)
    await limiter.hit(KEY, window_seconds=60)
    clock.advance(59.5)

    last = await limiter.hit(KEY, window_seconds=60)
    clock.advance(0.5)
    after = await limiter.peek(KEY)
    fresh = await limiter.hit(KEY, window_seconds=60)

    # Half a second left is one whole second to wait, as Retry-After counts it.
    assert last == WindowCount(2, 1)
    assert after == WindowCount(0, 0)
    assert fresh == WindowCount(1, 60)
    assert limiter.windows == {KEY: (1, clock.now() + timedelta(seconds=60))}


async def test_scripted_failures_count_nothing() -> None:
    limiter = InMemoryRateLimiter(FakeClock())
    limiter.fail_next(2)

    with pytest.raises(ServiceUnavailable):
        await limiter.hit(KEY, window_seconds=60)
    with pytest.raises(ServiceUnavailable):
        await limiter.peek(KEY)

    assert limiter.windows == {}
    assert (await limiter.hit(KEY, window_seconds=60)).hits == 1


async def test_a_window_is_at_least_a_second_long() -> None:
    with pytest.raises(ValueError, match="window_seconds must be positive"):
        await InMemoryRateLimiter(FakeClock()).hit(KEY, window_seconds=0)
