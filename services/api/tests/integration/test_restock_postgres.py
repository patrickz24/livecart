"""The idempotent restock on PostgreSQL and Redis: a replay with the same key adds nothing.

The store is ``RedisIdempotencyStore`` on database 15, under a prefix of this
test alone; the stock is the ``products`` row of livecart_test, restocked by
the SQL adapter as livecart_app.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx2
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_api.app import create_app
from livecart_api.testing import (
    current_time_clock,
    make_settings,
    open_client,
    postgres_resources,
    seller_headers,
)
from livecart_core.application.dto import ProductRecord, StockLevel
from livecart_core.infrastructure.idempotency import RedisIdempotencyStore
from livecart_core.testing.backends import PostgresBackend

pytestmark = pytest.mark.integration

# What a client sends as its Idempotency-Key: one id per restock, the same on every retry.
ATTEMPT_ID = "0192e4a0-7c1d-7b3e-9a50-3f1c2d4e5f60"
IN_FLIGHT = {"detail": "The request of this Idempotency-Key has not answered yet: try again"}


@dataclass
class RestockApi:
    client: httpx2.AsyncClient
    backend: PostgresBackend
    headers: dict[str, str]
    product: ProductRecord
    path: str

    async def restock(self, delta: int, key: str = ATTEMPT_ID) -> httpx2.Response:
        return await self.client.post(
            self.path, json={"delta": delta}, headers={**self.headers, "Idempotency-Key": key}
        )

    async def stock(self) -> StockLevel:
        stored = await self.backend.products.get(self.product.id)
        assert stored is not None
        return stored.stock


@pytest.fixture
async def api(
    clean_database: None,
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
) -> AsyncIterator[RestockApi]:
    store = RedisIdempotencyStore(redis_client, prefix=redis_key_prefix)
    app = create_app(
        make_settings(),
        postgres_resources(app_engine, clock=current_time_clock(), idempotency=store),
    )
    backend = PostgresBackend(app_engine, migrator_engine)
    session = await backend.add_live_session()
    product = await backend.add_product(session.id, "Z99", stock_total=200)
    path = f"/api/v1/live-sessions/{session.id}/products/{product.id}/restock"
    async with open_client(app) as client:
        yield RestockApi(client, backend, seller_headers(app, session.seller_id), product, path)


async def test_a_replay_with_the_same_key_does_not_add_the_stock_twice(api: RestockApi) -> None:
    first = await api.restock(50)
    replayed = await api.restock(50)

    assert first.status_code == replayed.status_code == 200
    assert replayed.json() == first.json()
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert first.json()["stock_total"] == 250
    assert await api.stock() == StockLevel(250)


async def test_concurrent_retries_of_one_key_restock_once(api: RestockApi) -> None:
    answers = await asyncio.gather(*(api.restock(10) for _ in range(10)))

    ran = [
        answer
        for answer in answers
        if answer.status_code == 200 and "Idempotent-Replayed" not in answer.headers
    ]
    others = [answer for answer in answers if answer not in ran]
    assert len(ran) == 1
    # The others replayed its answer, or were told that it had not answered yet.
    for answer in others:
        assert answer.json() in (ran[0].json(), IN_FLIGHT)
    assert await api.stock() == StockLevel(210)


async def test_the_same_key_with_another_delta_is_refused(api: RestockApi) -> None:
    await api.restock(10)

    reused = await api.restock(-10)

    assert reused.status_code == 422
    assert await api.stock() == StockLevel(210)


async def test_a_restock_below_what_is_reserved_and_sold_is_409(api: RestockApi) -> None:
    await api.backend.set_stock(api.product.id, StockLevel(200, reserved=150, sold=40))

    refused = await api.restock(-11, key="too-much")
    fitted = await api.restock(-10, key="just-enough")

    assert refused.status_code == 409
    assert (fitted.status_code, fitted.json()["available"]) == (200, 0)
    assert await api.stock() == StockLevel(190, reserved=150, sold=40)
