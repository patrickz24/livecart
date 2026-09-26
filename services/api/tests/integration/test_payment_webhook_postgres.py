"""The payment webhook on livecart_test: recorded in PostgreSQL, with or without Redis.

The first tests run the API with a Redis nobody listens on, as when the
container is stopped: the mock checks its signature without Redis, the limit
of failed signatures fails open, and the event is recorded all the same. The
last one pays on the mock's hosted page, on a real Redis, and lets its
webhook reach the same app, as the loopback call of a running API does.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_api.app import create_app
from livecart_api.testing import make_settings, open_client, postgres_resources
from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    IntentSource,
    PaymentEvent,
    PaymentProviderName,
    ReservationRequest,
)
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.payments.mock import MockPaymentProvider
from livecart_core.infrastructure.ratelimit import RedisRateLimiter
from livecart_core.testing.backends import ORDER_TOKENS, PostgresBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START, FakeClock
from livecart_core.testing.keys import TEST_MOCK_PAYMENT_WEBHOOK_SECRET

pytestmark = pytest.mark.integration

MOCK_WEBHOOK = "/api/v1/webhooks/payments/mock"
WEBHOOK_URL = f"http://127.0.0.1:8000{MOCK_WEBHOOK}"
ORIGIN = "http://localhost:8080"


def _mock(redis: Redis, prefix: str, clock: FakeClock) -> MockPaymentProvider:
    return MockPaymentProvider(
        redis,
        prefix=prefix,
        secret=TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
        clock=clock,
        public_base_url=ORIGIN,
    )


async def _registered(migrator: AsyncEngine) -> list[dict[str, Any]]:
    events = tables.processed_webhook_events
    async with migrator.connect() as connection:
        rows = (await connection.execute(select(events).order_by(events.c.created_at))).all()
    return [dict(row._mapping) for row in rows]


@dataclass
class DownApi:
    client: httpx2.AsyncClient
    mock: MockPaymentProvider
    clock: FakeClock
    migrator: AsyncEngine


@pytest.fixture
async def api_without_redis(
    clean_database: None,
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    unreachable_redis_client: Redis,
) -> AsyncIterator[DownApi]:
    clock = FakeClock()
    mock = _mock(unreachable_redis_client, "down", clock)
    resources = postgres_resources(
        app_engine,
        clock=clock,
        rate_limiter=RedisRateLimiter(unreachable_redis_client, prefix="down"),
        payment_provider=mock,
    )
    app = create_app(make_settings(), resources)
    async with open_client(app) as client:
        yield DownApi(client, mock, clock, migrator_engine)


def _completed(clock: FakeClock) -> PaymentEvent:
    return PaymentEvent(
        provider=PaymentProviderName.MOCK,
        event_id="evt_mock_1",
        event_type=CheckoutEventType.COMPLETED.value,
        checkout_ref="mock_cs_1",
        payment_ref="mock_pi_1",
        payment_status=CheckoutPaymentStatus.PAID,
        amount_minor=2_500,
        currency="EUR",
        occurred_at=clock.now(),
    )


async def test_a_payment_webhook_is_recorded_while_redis_is_down(
    api_without_redis: DownApi,
) -> None:
    api = api_without_redis
    event = _completed(api.clock)
    headers, body = api.mock.signed_webhook(event)

    first = await api.client.post(MOCK_WEBHOOK, content=body, headers=headers)
    api.clock.advance(timedelta(seconds=30))
    again = await api.client.post(MOCK_WEBHOOK, content=body, headers=headers)

    assert (first.status_code, first.json()) == (200, {"redelivery": False})
    assert (again.status_code, again.json()) == (200, {"redelivery": True})
    [row] = await _registered(api.migrator)
    assert (row["provider"], row["event_id"], row["event_type"]) == (
        "mock",
        event.event_id,
        CheckoutEventType.COMPLETED.value,
    )
    assert row["payload"] == event.model_dump(mode="json")
    assert (row["delivery_count"], row["attempts"], row["processed_at"], row["outcome"]) == (
        2,
        0,
        None,
        None,
    )
    assert row["created_at"] == FAKE_CLOCK_START


async def test_a_forged_payment_webhook_is_refused_while_redis_is_down(
    api_without_redis: DownApi,
) -> None:
    api = api_without_redis
    _, body = api.mock.signed_webhook(_completed(api.clock))

    refused = await api.client.post(MOCK_WEBHOOK, content=body)

    assert refused.status_code == 401
    assert await _registered(api.migrator) == []


class LoopbackTransport(httpx2.AsyncBaseTransport):
    """Sends what the hosted page posts to the app itself, as its loopback URL does."""

    def __init__(self) -> None:
        self.inner: httpx2.AsyncBaseTransport | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if self.inner is None:
            raise RuntimeError("the app is not built yet")
        return await self.inner.handle_async_request(request)


async def test_paying_on_the_mock_page_records_its_event_through_the_webhook(
    clean_database: None,
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
) -> None:
    backend = PostgresBackend(app_engine, migrator_engine)
    session = await backend.add_live_session()
    await backend.add_product(session.id, "B07", price=Money(1_250, "EUR"), stock_total=4)
    comment_id = await backend.add_comment("comment-1")
    reserved = await backend.orders.reserve(
        ReservationRequest(
            comment_id=comment_id,
            session_id=session.id,
            product_code=ProductCode("B07"),
            variant=Variant(),
            quantity=Quantity(2),
            source=IntentSource.PARSER,
            confidence=Decimal("1.00"),
            evidence=("B07",),
            now=FAKE_CLOCK_START,
            reserved_until=FAKE_CLOCK_START + timedelta(seconds=900),
        )
    )
    assert reserved is not None
    assert reserved.order is not None
    order = reserved.order
    clock = FakeClock()
    mock = _mock(redis_client, redis_key_prefix, clock)
    loopback = LoopbackTransport()
    app = create_app(
        make_settings(mock_payment_webhook_url=WEBHOOK_URL),
        postgres_resources(app_engine, clock=clock, payment_provider=mock),
        mock_webhook_transport=loopback,
    )
    loopback.inner = httpx2.ASGITransport(app=app, client=("127.0.0.1", 50_001))
    bearer = {"Authorization": f"Bearer {ORDER_TOKENS.token_for(order.id)}"}
    path = f"/api/v1/orders/{order.reference}"
    address = {
        "full_name": "Ada Buyer",
        "line1": "1 rue du Live",
        "postal_code": "75001",
        "city": "Paris",
        "country": "FR",
    }

    async with open_client(app) as client:
        await client.put(f"{path}/shipping-address", json=address, headers=bearer)
        started = await client.post(
            f"{path}/checkout", json={"return_url": "livecart://payment-return"}, headers=bearer
        )
        hosted_page = started.json()["redirect_url"].removeprefix(ORIGIN)
        paid = await client.post(f"{hosted_page}/pay")
        paid_again = await client.post(f"{hosted_page}/pay")

    assert started.status_code == 200
    assert (paid.status_code, paid_again.status_code) == (303, 303)
    [row] = await _registered(migrator_engine)
    ref = hosted_page.rsplit("/", 1)[-1]
    assert (row["provider"], row["event_id"]) == ("mock", mock.event_id(ref))
    assert row["payload"]["checkout_ref"] == ref
    assert (row["payload"]["amount_minor"], row["payload"]["currency"]) == (2_500, "EUR")
    # Paying again posted the same event again: counted, not recorded twice.
    assert (row["delivery_count"], row["processed_at"]) == (2, None)
