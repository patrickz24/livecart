"""``RedisLlmBudget`` on the compose Redis: its keys, their expiry and its failures.

The counting rules themselves are the contract scenarios of
tests/contract/test_llm_budget.py, which run here on Redis too.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from livecart_core.application.ports import ServiceUnavailable
from livecart_core.infrastructure.llm_budget import COUNTER_TTL_SECONDS, RedisLlmBudget
from livecart_core.infrastructure.redis_client import create_redis_client

pytestmark = pytest.mark.integration

SESSION = UUID("0192f0c1-0000-7000-8000-000000000001")
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


async def test_the_counters_live_under_the_prefix_and_expire(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    budget = RedisLlmBudget(redis_client, prefix=redis_key_prefix)

    await budget.count_call(SESSION, now=NOW)
    await budget.add_spend(Decimal("0.0123"), now=NOW)

    calls = f"{redis_key_prefix}:llm:calls:{SESSION}"
    spend = f"{redis_key_prefix}:llm:spend:2026-09-26"
    assert (budget.calls_key(SESSION), budget.spend_key(NOW)) == (calls, spend)
    assert (await redis_client.get(calls), await redis_client.get(spend)) == (b"1", b"12300")
    for key in (calls, spend):
        assert 0 < await redis_client.ttl(key) <= COUNTER_TTL_SECONDS


async def test_a_later_call_never_pushes_the_expiry_back(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    budget = RedisLlmBudget(redis_client, prefix=redis_key_prefix)
    await budget.count_call(SESSION, now=NOW)
    key = budget.calls_key(SESSION)
    await redis_client.expire(key, 60)

    await budget.count_call(SESSION, now=NOW)

    assert 0 < await redis_client.ttl(key) <= 60


async def test_an_unreachable_redis_is_service_unavailable() -> None:
    # A closed port on the loopback: nothing listens.
    client = create_redis_client(SecretStr("redis://127.0.0.1:1/0"))
    budget = RedisLlmBudget(client, prefix="test")
    try:
        with pytest.raises(ServiceUnavailable, match="llm budget unavailable"):
            await budget.usage(SESSION, now=NOW)
        with pytest.raises(ServiceUnavailable, match="llm budget unavailable"):
            await budget.add_spend(Decimal("0.01"), now=NOW)
    finally:
        await client.aclose()
