"""``IdempotencyStore`` on Redis: one key per seller and idempotency key, for a day.

The key is ``{prefix}:idem:{scope}:{seller_id}:{key}``: the seller's id
keeps two sellers who pick the same key apart, and the prefix keeps each
deployment (and each test) apart. Its value is the JSON of an
``IdempotencyRecord``.

- ``claim`` is ``SET key <pending record> NX EX 86400``: one command, so of
  several requests under one key exactly one sets it. A request that finds
  it set reads it with ``GET``; when the key expired in between, it tries
  the ``SET NX`` again;
- ``save`` is ``SET key <record with the answer> XX KEEPTTL``: the answer
  replaces the claim and keeps its expiry, a day after the first request,
  and never recreates a key that was released meanwhile;
- ``release`` is ``DEL``.

Any Redis error raises ``ServiceUnavailable``: without the store, a restock
cannot promise to run once, so it does not run (503).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from redis.exceptions import RedisError

from livecart_core.application.dto import IdempotencyKey, IdempotencyRecord
from livecart_core.application.ports import IDEMPOTENCY_TTL_SECONDS, ServiceUnavailable

# A claim that expires between the failed SET NX and the GET is claimed again, once.
CLAIM_TRIES = 2


@asynccontextmanager
async def _redis_errors() -> AsyncIterator[None]:
    try:
        yield
    except RedisError as error:
        raise ServiceUnavailable(
            f"idempotency store unavailable: {type(error).__name__}"
        ) from error


class RedisIdempotencyStore:
    """Idempotent answers in Redis; see the module docstring."""

    def __init__(
        self, client: Redis, *, prefix: str, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._ttl_seconds = ttl_seconds

    def key_name(self, key: IdempotencyKey) -> str:
        return f"{self._prefix}:idem:{key.scope}:{key.seller_id}:{key.value}"

    async def claim(self, key: IdempotencyKey, fingerprint: str) -> IdempotencyRecord | None:
        name = self.key_name(key)
        pending = IdempotencyRecord(fingerprint=fingerprint).model_dump_json()
        async with _redis_errors():
            for _ in range(CLAIM_TRIES):
                if await self._client.set(name, pending, nx=True, ex=self._ttl_seconds):
                    return None
                stored = await self._client.get(name)
                if stored is not None:
                    return IdempotencyRecord.model_validate_json(stored)
        raise ServiceUnavailable("the idempotency key kept expiring while it was claimed")

    async def save(self, key: IdempotencyKey, record: IdempotencyRecord) -> None:
        async with _redis_errors():
            await self._client.set(
                self.key_name(key), record.model_dump_json(), xx=True, keepttl=True
            )

    async def release(self, key: IdempotencyKey) -> None:
        async with _redis_errors():
            await self._client.delete(self.key_name(key))
