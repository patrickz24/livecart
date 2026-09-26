"""What ``RedisRateLimiter`` does on Redis beyond the scenarios of tests/contract.

The key's name and expiry, the end of a window in real time, a key left
without an expiry, and a Redis that cannot be reached.
"""

import asyncio

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from livecart_core.application.dto import RateLimitKey, WindowCount
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.infrastructure.ratelimit import RedisRateLimiter
from livecart_core.infrastructure.redis_client import create_redis_client

KEY = RateLimitKey("login", "203.0.113.7")


@pytest.mark.integration
async def test_a_counter_is_one_key_that_expires_with_its_window(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    limiter = RedisRateLimiter(redis_client, prefix=redis_key_prefix)
    name = f"{redis_key_prefix}:rl:login:203.0.113.7"

    await limiter.hit(KEY, window_seconds=900)
    await limiter.hit(KEY, window_seconds=900)

    assert limiter.key_name(KEY) == name
    assert await redis_client.get(name) == b"2"
    assert 895_000 <= await redis_client.pttl(name) <= 900_000


@pytest.mark.integration
async def test_the_next_hit_after_a_window_starts_a_new_one(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    limiter = RedisRateLimiter(redis_client, prefix=redis_key_prefix)
    await limiter.hit(KEY, window_seconds=1)
    await limiter.hit(KEY, window_seconds=1)

    await asyncio.sleep(1.1)

    assert await limiter.peek(KEY) == WindowCount(0, 0)
    assert await limiter.hit(KEY, window_seconds=1) == WindowCount(1, 1)


@pytest.mark.integration
async def test_a_counter_left_without_expiry_gets_one_at_its_next_hit(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    limiter = RedisRateLimiter(redis_client, prefix=redis_key_prefix)
    await redis_client.set(limiter.key_name(KEY), 7)

    before = await limiter.peek(KEY)
    after = await limiter.hit(KEY, window_seconds=60)

    # A counter that never expired would lock its subject out for good.
    assert before == WindowCount(7, 0)
    assert after == WindowCount(8, 60)


async def test_an_unreachable_redis_is_service_unavailable() -> None:
    # A closed port on the loopback: no Docker needed, and nothing listens.
    client = create_redis_client(SecretStr("redis://127.0.0.1:1/0"))
    limiter = RedisRateLimiter(client, prefix="test")
    try:
        with pytest.raises(ServiceUnavailable, match="ConnectionError"):
            await limiter.hit(KEY, window_seconds=60)
        with pytest.raises(ServiceUnavailable):
            await limiter.peek(KEY)
        with pytest.raises(ValueError, match="window_seconds"):
            await limiter.hit(KEY, window_seconds=0)
    finally:
        await client.aclose()
