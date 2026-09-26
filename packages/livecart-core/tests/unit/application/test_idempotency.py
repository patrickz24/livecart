"""``IdempotentRequests`` over the in-memory store: run once, replay, refuse, release."""

from uuid import UUID

import pytest
from pydantic import JsonValue

from livecart_core.application.dto import IdempotencyKey, StoredResponse
from livecart_core.application.idempotency import (
    IdempotencyKeyReused,
    IdempotentRequestInFlight,
    IdempotentRequests,
    fingerprint,
)
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.testing.fakes import InMemoryIdempotencyStore

KEY = IdempotencyKey("restock", UUID(int=1), "key-1")
REQUEST: dict[str, JsonValue] = {"product_id": "p-1", "delta": 5}
OK = StoredResponse(status_code=200, body={"stock_total": 15})


class Action:
    """Counts its runs and answers what it was told to."""

    def __init__(self, answer: StoredResponse = OK, *, fails: bool = False) -> None:
        self.answer = answer
        self.fails = fails
        self.runs = 0

    async def __call__(self) -> StoredResponse:
        self.runs += 1
        if self.fails:
            raise ServiceUnavailable("database unavailable")
        return self.answer


async def test_the_first_request_runs_and_its_repeat_replays_its_answer() -> None:
    requests, action = IdempotentRequests(InMemoryIdempotencyStore()), Action()

    first = await requests.run(KEY, REQUEST, action)
    again = await requests.run(KEY, dict(reversed(REQUEST.items())), action)

    assert (first.response, first.replayed) == (OK, False)
    assert (again.response, again.replayed) == (OK, True)
    assert action.runs == 1


async def test_a_refusal_is_replayed_like_a_success() -> None:
    requests = IdempotentRequests(InMemoryIdempotencyStore())
    refused = StoredResponse(status_code=409, body={"detail": "below reserved and sold"})

    await requests.run(KEY, REQUEST, Action(refused))
    again = await requests.run(KEY, REQUEST, Action())

    assert (again.response, again.replayed) == (refused, True)


async def test_a_key_reused_for_another_request_is_refused() -> None:
    requests, action = IdempotentRequests(InMemoryIdempotencyStore()), Action()
    await requests.run(KEY, REQUEST, action)

    with pytest.raises(IdempotencyKeyReused):
        await requests.run(KEY, {**REQUEST, "delta": 6}, action)

    assert action.runs == 1


async def test_a_key_whose_request_has_not_answered_is_in_flight() -> None:
    store = InMemoryIdempotencyStore()
    await store.claim(KEY, fingerprint(REQUEST))

    with pytest.raises(IdempotentRequestInFlight):
        await IdempotentRequests(store).run(KEY, REQUEST, Action())


async def test_a_request_that_failed_releases_its_key() -> None:
    store = InMemoryIdempotencyStore()
    requests = IdempotentRequests(store)

    with pytest.raises(ServiceUnavailable):
        await requests.run(KEY, REQUEST, Action(fails=True))
    retried = await requests.run(KEY, REQUEST, Action())

    assert (retried.response, retried.replayed) == (OK, False)
    assert store.records[KEY].response == OK


async def test_a_store_that_fails_to_claim_runs_nothing() -> None:
    store, action = InMemoryIdempotencyStore(), Action()
    store.fail_next()

    with pytest.raises(ServiceUnavailable):
        await IdempotentRequests(store).run(KEY, REQUEST, action)

    assert action.runs == 0


def test_a_fingerprint_is_the_sha256_of_the_canonical_json() -> None:
    assert fingerprint({"b": 1, "a": "é"}) == fingerprint({"a": "é", "b": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    # sha256(b'{"a":1}')
    assert fingerprint({"a": 1}) == (
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )


@pytest.mark.parametrize(
    ("scope", "value"),
    [("restock", ""), ("restock", "x" * 256), ("restock", "with space"), ("Re stock", "k")],
)
def test_an_idempotency_key_is_bounded_visible_ascii(scope: str, value: str) -> None:
    with pytest.raises(ValueError, match="idempotency"):
        IdempotencyKey(scope, UUID(int=1), value)
