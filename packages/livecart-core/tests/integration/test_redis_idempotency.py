"""What ``RedisIdempotencyStore`` does on Redis beyond the scenarios of tests/contract.

The key's name and its day of life, kept by the answer that replaces the
claim; and a Redis that cannot be reached, which is ``ServiceUnavailable``.
"""

from uuid import UUID

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from livecart_core.application.dto import IdempotencyKey, IdempotencyRecord, StoredResponse
from livecart_core.application.ports import IDEMPOTENCY_TTL_SECONDS, ServiceUnavailable
from livecart_core.infrastructure.idempotency import RedisIdempotencyStore
from livecart_core.infrastructure.redis_client import create_redis_client

SELLER = UUID("0192e4a0-0000-7000-8000-000000000001")
KEY = IdempotencyKey("restock", SELLER, "3f1c-key")
FINGERPRINT = "c" * 64


@pytest.mark.integration
async def test_a_claim_is_one_key_per_seller_that_lives_a_day(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    store = RedisIdempotencyStore(redis_client, prefix=redis_key_prefix)
    name = f"{redis_key_prefix}:idem:restock:{SELLER}:3f1c-key"

    await store.claim(KEY, FINGERPRINT)

    stored = await redis_client.get(name)
    assert store.key_name(KEY) == name
    assert stored is not None
    assert IdempotencyRecord.model_validate_json(stored) == IdempotencyRecord(
        fingerprint=FINGERPRINT
    )
    assert IDEMPOTENCY_TTL_SECONDS - 5 <= await redis_client.ttl(name) <= IDEMPOTENCY_TTL_SECONDS


@pytest.mark.integration
async def test_the_answer_keeps_the_expiry_of_the_claim(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    store = RedisIdempotencyStore(redis_client, prefix=redis_key_prefix)
    await store.claim(KEY, FINGERPRINT)
    name = store.key_name(KEY)
    await redis_client.expire(name, 600)
    answered = IdempotencyRecord(
        fingerprint=FINGERPRINT, response=StoredResponse(status_code=409, body={"detail": "no"})
    )

    await store.save(KEY, answered)

    assert 590 <= await redis_client.ttl(name) <= 600
    assert await store.claim(KEY, FINGERPRINT) == answered


@pytest.mark.integration
async def test_a_released_claim_leaves_no_key(redis_client: Redis, redis_key_prefix: str) -> None:
    store = RedisIdempotencyStore(redis_client, prefix=redis_key_prefix)
    await store.claim(KEY, FINGERPRINT)

    await store.release(KEY)

    assert await redis_client.exists(store.key_name(KEY)) == 0


async def test_an_unreachable_redis_is_service_unavailable() -> None:
    # A closed port on the loopback: no Docker needed, and nothing listens.
    client = create_redis_client(SecretStr("redis://127.0.0.1:1/0"))
    store = RedisIdempotencyStore(client, prefix="test")
    try:
        with pytest.raises(ServiceUnavailable, match="ConnectionError"):
            await store.claim(KEY, FINGERPRINT)
        with pytest.raises(ServiceUnavailable):
            await store.release(KEY)
    finally:
        await client.aclose()
