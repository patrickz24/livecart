"""``RateLimiter`` on every backend: the in-memory fake, then Redis.

The end of a window needs time to pass, which the fake's clock and Redis
measure differently: tests/unit/fakes and tests/integration prove it apart.
"""

import asyncio

from livecart_core.application.dto import RateLimitKey, WindowCount
from livecart_core.application.ports import RateLimiter

KEY = RateLimitKey("login", "203.0.113.7")
WINDOW = 60


async def test_each_hit_counts_itself_within_the_window(rate_limiter: RateLimiter) -> None:
    counts = [await rate_limiter.hit(KEY, window_seconds=WINDOW) for _ in range(3)]

    assert [count.hits for count in counts] == [1, 2, 3]
    assert all(0 < count.resets_in_seconds <= WINDOW for count in counts)


async def test_a_peek_reads_the_running_window_without_counting(
    rate_limiter: RateLimiter,
) -> None:
    before = await rate_limiter.peek(KEY)
    await rate_limiter.hit(KEY, window_seconds=WINDOW)
    await rate_limiter.hit(KEY, window_seconds=WINDOW)

    during = await rate_limiter.peek(KEY)
    again = await rate_limiter.peek(KEY)

    assert before == WindowCount(0, 0)
    assert during.hits == again.hits == 2
    assert 0 < during.resets_in_seconds <= WINDOW


async def test_counters_are_apart_per_rule_and_subject(rate_limiter: RateLimiter) -> None:
    await rate_limiter.hit(KEY, window_seconds=WINDOW)
    await rate_limiter.hit(KEY, window_seconds=WINDOW)

    other_subject = await rate_limiter.hit(RateLimitKey("login", "198.51.100.1"), window_seconds=60)
    other_rule = await rate_limiter.hit(RateLimitKey("seller", "203.0.113.7"), window_seconds=60)

    assert (other_subject.hits, other_rule.hits) == (1, 1)
    assert (await rate_limiter.peek(KEY)).hits == 2


async def test_a_later_hit_never_extends_the_window(rate_limiter: RateLimiter) -> None:
    await rate_limiter.hit(KEY, window_seconds=WINDOW)

    later = await rate_limiter.hit(KEY, window_seconds=3600)

    assert later.hits == 2
    assert later.resets_in_seconds <= WINDOW


async def test_concurrent_hits_each_get_a_count_of_their_own(rate_limiter: RateLimiter) -> None:
    counts = await asyncio.gather(
        *(rate_limiter.hit(KEY, window_seconds=WINDOW) for _ in range(50))
    )

    assert sorted(count.hits for count in counts) == list(range(1, 51))
