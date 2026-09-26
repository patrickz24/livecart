"""``IdempotencyStore`` on every backend: the in-memory fake, then Redis."""

import asyncio
from uuid import UUID

from livecart_core.application.dto import IdempotencyKey, IdempotencyRecord, StoredResponse
from livecart_core.application.ports import IdempotencyStore

SELLER = UUID(int=1)
FIRST = "a" * 64
OTHER = "b" * 64


def _key(value: str = "key-1", *, seller_id: UUID = SELLER) -> IdempotencyKey:
    return IdempotencyKey("restock", seller_id, value)


def _answered(fingerprint: str = FIRST) -> IdempotencyRecord:
    return IdempotencyRecord(
        fingerprint=fingerprint,
        response=StoredResponse(status_code=200, body={"id": "p-1", "stock_total": 12}),
    )


async def test_the_first_claim_wins_and_the_next_read_it(
    idempotency_store: IdempotencyStore,
) -> None:
    first = await idempotency_store.claim(_key(), FIRST)
    second = await idempotency_store.claim(_key(), OTHER)

    assert first is None
    assert second == IdempotencyRecord(fingerprint=FIRST)


async def test_a_saved_answer_is_what_the_next_claims_read(
    idempotency_store: IdempotencyStore,
) -> None:
    await idempotency_store.claim(_key(), FIRST)

    await idempotency_store.save(_key(), _answered())

    assert await idempotency_store.claim(_key(), FIRST) == _answered()


async def test_a_released_key_may_be_claimed_again(idempotency_store: IdempotencyStore) -> None:
    await idempotency_store.claim(_key(), FIRST)

    await idempotency_store.release(_key())
    # Saving after a release recreates nothing.
    await idempotency_store.save(_key(), _answered())

    assert await idempotency_store.claim(_key(), OTHER) is None
    assert await idempotency_store.claim(_key(), FIRST) == IdempotencyRecord(fingerprint=OTHER)


async def test_keys_are_apart_per_seller_scope_and_value(
    idempotency_store: IdempotencyStore,
) -> None:
    await idempotency_store.claim(_key(), FIRST)

    assert await idempotency_store.claim(_key(seller_id=UUID(int=2)), FIRST) is None
    assert await idempotency_store.claim(IdempotencyKey("refund", SELLER, "key-1"), FIRST) is None
    assert await idempotency_store.claim(_key("key-2"), FIRST) is None


async def test_one_of_many_concurrent_claims_wins(idempotency_store: IdempotencyStore) -> None:
    claims = await asyncio.gather(
        *(idempotency_store.claim(_key(), f"{n:064x}") for n in range(20))
    )

    winners = [n for n, claim in enumerate(claims) if claim is None]
    assert len(winners) == 1
    readers = [claim for claim in claims if claim is not None]
    assert {claim.fingerprint for claim in readers} == {f"{winners[0]:064x}"}
