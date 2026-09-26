"""The rate limits of the API on a real Redis: every process counts in the same windows.

Two apps stand for two API replicas behind nginx; the repositories are the
fakes, since only the counters matter here.
"""

import pytest
from redis.asyncio import Redis

from livecart_api.testing import (
    current_time_clock,
    make_app,
    make_settings,
    open_client,
)
from livecart_core.infrastructure.ratelimit import RedisRateLimiter
from livecart_core.settings import AppEnv
from livecart_core.testing.fakes import InMemoryDatabase

pytestmark = pytest.mark.integration

REFERENCE = "/api/v1/orders/LC-ZZZZZZ"


async def test_two_api_processes_share_the_counters_of_one_address(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    settings = make_settings(AppEnv.DEVELOPMENT, rate_limit_buyer_lookup_per_minute=3)
    replicas = [
        make_app(
            database=InMemoryDatabase(),
            clock=current_time_clock(),
            rate_limiter=RedisRateLimiter(redis_client, prefix=redis_key_prefix),
            settings=settings,
        )
        for _ in range(2)
    ]

    statuses = []
    for n in range(4):
        async with open_client(replicas[n % 2], peer="203.0.113.7") as client:
            response = await client.get(REFERENCE)
            statuses.append(response.status_code)
    async with open_client(replicas[0], peer="198.51.100.1") as other:
        elsewhere = await other.get(REFERENCE)

    assert statuses == [404, 404, 404, 429]
    assert 0 < int(response.headers["Retry-After"]) <= 60
    assert elsewhere.status_code == 404
    assert await redis_client.get(f"{redis_key_prefix}:rl:buyer_lookup:203.0.113.7") == b"4"
