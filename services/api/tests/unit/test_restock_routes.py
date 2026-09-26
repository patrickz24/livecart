"""The idempotent restock route over the fakes: once per key, the first answer replayed.

``tests/integration/test_restock_postgres.py`` runs it on PostgreSQL and Redis,
concurrent retries included.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import JsonValue

from livecart_api.app import create_app
from livecart_api.testing import make_settings, memory_resources, open_client, seller_headers
from livecart_core.application.dto import (
    IdempotencyKey,
    LiveSessionRecord,
    ProductRecord,
    StockLevel,
)
from livecart_core.application.idempotency import fingerprint
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.testing.fakes import (
    InMemoryDatabase,
    InMemoryIdempotencyStore,
    InMemoryProductStockRepository,
)

# What a client sends as its Idempotency-Key: one id per restock, the same on every retry.
ATTEMPT_ID = "0192e4a0-7c1d-7b3e-9a50-3f1c2d4e5f60"
REPLAYED = "Idempotent-Replayed"


class FlakyProducts(InMemoryProductStockRepository):
    """The fake products, whose first restock fails as a database outage would."""

    failures = 1

    async def restock(
        self, seller_id: UUID, session_id: UUID, product_id: UUID, delta: int
    ) -> ProductRecord:
        if self.failures:
            self.failures -= 1
            raise ServiceUnavailable("database unavailable: TimeoutError")
        return await super().restock(seller_id, session_id, product_id, delta)


@dataclass
class Api:
    app: FastAPI
    client: httpx2.AsyncClient
    database: InMemoryDatabase
    store: InMemoryIdempotencyStore
    seller_id: UUID
    headers: dict[str, str]
    session: LiveSessionRecord
    product: ProductRecord

    def path(self, product_id: UUID | None = None) -> str:
        product = product_id or self.product.id
        return f"/api/v1/live-sessions/{self.session.id}/products/{product}/restock"

    async def restock(
        self,
        body: object,
        key: str | None = ATTEMPT_ID,
        *,
        product_id: UUID | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx2.Response:
        sent = dict(headers if headers is not None else self.headers)
        if key is not None:
            sent["Idempotency-Key"] = key
        return await self.client.post(self.path(product_id), json=body, headers=sent)

    def stock(self) -> StockLevel:
        return self.database.tables.products[self.product.id].stock


@asynccontextmanager
async def _open(*, flaky: bool = False) -> AsyncIterator[Api]:
    database, store = InMemoryDatabase(), InMemoryIdempotencyStore()
    resources = memory_resources(database, idempotency=store)
    if flaky:
        resources = replace(resources, products=FlakyProducts(database))
    app = create_app(make_settings(), resources)
    seller = database.add_seller()
    session = database.add_live_session(seller_id=seller.id)
    product = database.add_product(session.id, "Z99", stock_total=10)
    async with open_client(app) as client:
        yield Api(
            app,
            client,
            database,
            store,
            seller.id,
            seller_headers(app, seller.id),
            session,
            product,
        )


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with _open() as opened:
        yield opened


# --- Once per key ----------------------------------------------------------------------


async def test_a_restock_adds_the_units_and_answers_the_product(api: Api) -> None:
    response = await api.restock({"delta": 5})

    assert response.status_code == 200
    assert REPLAYED not in response.headers
    body = response.json()
    assert (body["id"], body["stock_total"], body["available"]) == (str(api.product.id), 15, 15)
    assert api.stock() == StockLevel(15)


async def test_a_repeated_request_adds_nothing_and_replays_the_first_answer(api: Api) -> None:
    first = await api.restock({"delta": 5})

    again = await api.restock({"delta": 5})
    once_more = await api.restock({"delta": 5})

    assert again.status_code == once_more.status_code == 200
    assert again.json() == once_more.json() == first.json()
    assert again.headers[REPLAYED] == once_more.headers[REPLAYED] == "true"
    assert api.stock() == StockLevel(15)


async def test_a_new_key_is_a_new_restock(api: Api) -> None:
    await api.restock({"delta": 5})

    response = await api.restock({"delta": 5}, key="another-key")

    assert response.json()["stock_total"] == 20
    assert api.stock() == StockLevel(20)


@pytest.mark.parametrize("change", ["delta", "product"])
async def test_a_key_sent_with_another_request_is_refused(api: Api, change: str) -> None:
    other = api.database.add_product(api.session.id, "Z98", stock_total=10)
    await api.restock({"delta": 5})

    if change == "delta":
        response = await api.restock({"delta": 6})
    else:
        response = await api.restock({"delta": 5}, product_id=other.id)

    assert response.status_code == 422
    assert response.json() == {"detail": "This Idempotency-Key was sent with another request"}
    assert api.stock() == StockLevel(15)
    assert api.database.tables.products[other.id].stock == StockLevel(10)


async def test_two_sellers_may_pick_the_same_key(api: Api) -> None:
    other = api.database.add_seller()
    session = api.database.add_live_session(seller_id=other.id, external_live_id="live-2")
    product = api.database.add_product(session.id, "Z99", stock_total=1)
    other_headers = seller_headers(api.app, other.id)

    mine = await api.restock({"delta": 5})
    theirs = await api.client.post(
        f"/api/v1/live-sessions/{session.id}/products/{product.id}/restock",
        json={"delta": 2},
        headers={**other_headers, "Idempotency-Key": ATTEMPT_ID},
    )

    assert (mine.status_code, theirs.status_code) == (200, 200)
    assert REPLAYED not in theirs.headers
    assert theirs.json()["stock_total"] == 3


# --- Refusals are answers too ----------------------------------------------------------


async def test_a_refused_restock_is_replayed_as_it_was_first_answered(api: Api) -> None:
    api.database.tables.products[api.product.id] = replace(
        api.database.tables.products[api.product.id], stock=StockLevel(10, reserved=4, sold=2)
    )

    refused = await api.restock({"delta": -5})
    await api.restock({"delta": 10}, key="make-room")
    replayed = await api.restock({"delta": -5})

    assert refused.status_code == replayed.status_code == 409
    assert (
        refused.json()
        == replayed.json()
        == {"detail": "The stock total would fall below the units reserved and sold"}
    )
    assert replayed.headers[REPLAYED] == "true"
    # The replay did not run against the newer stock, where -5 would have fitted.
    assert api.stock() == StockLevel(20, reserved=4, sold=2)


async def test_a_restock_above_the_maximum_is_refused(api: Api) -> None:
    response = await api.restock({"delta": 1_000_000})

    assert response.status_code == 409
    assert response.json() == {"detail": "The stock total would rise above 1000000"}


async def test_an_ended_session_takes_no_restock(api: Api) -> None:
    sessions = api.database.tables.sessions
    sessions[api.session.id] = replace(sessions[api.session.id], status=LiveSessionStatus.ENDED)

    response = await api.restock({"delta": 1})

    assert response.status_code == 409
    assert response.json() == {"detail": "The session is ended: it can no longer change"}
    assert api.stock() == StockLevel(10)


async def test_an_unknown_product_is_404_and_stays_404_under_its_key(api: Api) -> None:
    first = await api.restock({"delta": 1}, product_id=UUID(int=404))
    again = await api.restock({"delta": 1}, product_id=UUID(int=404))

    assert first.status_code == again.status_code == 404
    assert first.json() == again.json() == {"detail": "Not found"}


async def test_a_key_whose_request_has_not_answered_is_409(api: Api) -> None:
    request: dict[str, JsonValue] = {
        "session_id": str(api.session.id),
        "product_id": str(api.product.id),
        "delta": 5,
    }
    await api.store.claim(
        IdempotencyKey("restock", api.seller_id, ATTEMPT_ID), fingerprint(request)
    )

    response = await api.restock({"delta": 5})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "The request of this Idempotency-Key has not answered yet: try again"
    }
    assert api.stock() == StockLevel(10)


# --- Validation and outages ------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ({"delta": 1}, None),
        ({"delta": 1}, ""),
        ({"delta": 1}, "with space"),
        ({"delta": 1}, "k" * 256),
        ({"delta": 0}, ATTEMPT_ID),
        ({"delta": 1_000_001}, ATTEMPT_ID),
        ({"delta": -1_000_001}, ATTEMPT_ID),
        ({"delta": 1.5}, ATTEMPT_ID),
        ({"delta": 1, "stock_total": 50}, ATTEMPT_ID),
        ({}, ATTEMPT_ID),
    ],
    ids=[
        "no_key",
        "empty_key",
        "spaced_key",
        "long_key",
        "zero",
        "too_many",
        "too_few",
        "fraction",
        "unknown_field",
        "no_delta",
    ],
)
async def test_a_bad_key_or_body_is_refused_before_anything_is_claimed(
    api: Api, body: dict[str, object], key: str | None
) -> None:
    response = await api.restock(body, key)

    assert response.status_code == 422
    assert api.store.records == {}
    assert api.stock() == StockLevel(10)


async def test_a_failed_restock_releases_its_key_for_the_retry() -> None:
    async with _open(flaky=True) as api:
        failed = await api.restock({"delta": 5})
        retried = await api.restock({"delta": 5})

    assert failed.status_code == 503
    assert failed.headers["Retry-After"] == "1"
    assert (retried.status_code, retried.json()["stock_total"]) == (200, 15)
    assert REPLAYED not in retried.headers
    assert api.stock() == StockLevel(15)


async def test_an_unavailable_store_restocks_nothing(api: Api) -> None:
    api.store.fail_next()

    response = await api.restock({"delta": 5})

    assert response.status_code == 503
    assert api.stock() == StockLevel(10)
