"""The buyer's checkout on the SQL adapters and livecart_test, with the mock provider on Redis.

The journey of the buyer app: the address, the checkout, the mock's hosted
page opened from the answer, then Pay, which posts the signed webhook,
recorded here by a fake transport, and sends the buyer to the bridge page,
whose button opens the app again.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import httpx2
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_api.app import create_app
from livecart_api.testing import make_settings, open_client, postgres_resources
from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutStatus,
    IntentSource,
    OrderRecord,
    ReservationRequest,
)
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.payments.mock import MockPaymentProvider
from livecart_core.testing.backends import ORDER_TOKENS, PostgresBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START, FakeClock
from livecart_core.testing.keys import TEST_MOCK_PAYMENT_WEBHOOK_SECRET

pytestmark = pytest.mark.integration

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
ORIGIN = "http://localhost:8080"
WEBHOOK_URL = "http://127.0.0.1:8000/api/v1/webhooks/payments/mock"
ADDRESS = {
    "full_name": "Ada Buyer",
    "line1": "1 rue du Live",
    "postal_code": "75001",
    "city": "Paris",
    "country": "FR",
}
RETURN = {"return_url": "livecart://payment-return"}


@dataclass
class CheckoutApi:
    client: httpx2.AsyncClient
    migrator: AsyncEngine
    mock: MockPaymentProvider
    clock: FakeClock
    order: OrderRecord
    webhooks: list[httpx2.Request] = field(default_factory=list)

    @property
    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {ORDER_TOKENS.token_for(self.order.id)}"}

    def path(self, action: str) -> str:
        return f"/api/v1/orders/{self.order.reference}/{action}"

    async def rows(self, table: str) -> list[dict[str, object]]:
        """The rows of the order in ``table``, read as the migrator."""
        selected = tables.metadata.tables[table]
        column = selected.c.id if table == "orders" else selected.c.order_id
        async with self.migrator.connect() as connection:
            rows = (await connection.execute(select(selected).where(column == self.order.id))).all()
        return [dict(row._mapping) for row in rows]


async def _reserve(backend: PostgresBackend, session_id: UUID, buyer: str) -> OrderRecord:
    comment_id = await backend.add_comment(f"comment-{buyer}", author=buyer)
    result = await backend.orders.reserve(
        ReservationRequest(
            comment_id=comment_id,
            session_id=session_id,
            product_code=ProductCode("B07"),
            variant=Variant(),
            quantity=Quantity(2),
            source=IntentSource.PARSER,
            confidence=Decimal("1.00"),
            evidence=("B07",),
            now=NOW,
            reserved_until=NOW + TTL,
        )
    )
    assert result is not None
    assert result.order is not None
    return result.order


@pytest.fixture
async def api(
    clean_database: None,
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
) -> AsyncIterator[CheckoutApi]:
    backend = PostgresBackend(app_engine, migrator_engine)
    session = await backend.add_live_session()
    await backend.add_product(session.id, "B07", price=Money(1_250, "EUR"), stock_total=4)
    order = await _reserve(backend, session.id, "buyer-a")
    clock = FakeClock()
    mock = MockPaymentProvider(
        redis_client,
        prefix=redis_key_prefix,
        secret=TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
        clock=clock,
        public_base_url=ORIGIN,
    )
    webhooks: list[httpx2.Request] = []

    def receive(request: httpx2.Request) -> httpx2.Response:
        webhooks.append(request)
        return httpx2.Response(200, json={"received": True})

    app = create_app(
        make_settings(mock_payment_webhook_url=WEBHOOK_URL),
        postgres_resources(app_engine, clock=clock, payment_provider=mock),
        mock_webhook_transport=httpx2.MockTransport(receive),
    )
    async with open_client(app) as client:
        yield CheckoutApi(client, migrator_engine, mock, clock, order, webhooks)


async def test_the_buyer_pays_through_the_mock_from_the_address_to_the_bridge(
    api: CheckoutApi,
) -> None:
    address = await api.client.put(api.path("shipping-address"), json=ADDRESS, headers=api.bearer)
    api.clock.advance(TTL - timedelta(seconds=60))

    started = await api.client.post(api.path("checkout"), json=RETURN, headers=api.bearer)

    assert address.status_code == 200
    [order] = await api.rows("orders")
    assert order["shipping_address"] == {**ADDRESS, "line2": ""}
    assert started.status_code == 200
    redirect_url = started.json()["redirect_url"]
    assert started.json()["attempt_no"] == 1
    assert redirect_url.startswith(f"{ORIGIN}/api/v1/mock-payments/mock_cs_")
    # The extension was committed with the attempt, pending, and its session recorded.
    assert order["reserved_until"] == api.clock.now() + timedelta(seconds=600)
    [payment] = await api.rows("payments")
    assert (payment["attempt_no"], payment["status"], payment["provider"]) == (1, "pending", "mock")
    assert (payment["amount_minor"], payment["currency"]) == (2_500, "EUR")
    assert payment["return_url"] == RETURN["return_url"]
    assert payment["redirect_url"] == redirect_url
    assert redirect_url.endswith(str(payment["provider_ref"]))

    page = await api.client.get(redirect_url.removeprefix(ORIGIN))
    paid = await api.client.post(f"{redirect_url.removeprefix(ORIGIN)}/pay")

    assert page.status_code == 200
    assert api.order.reference.value in page.text
    assert "25.00 EUR" in page.text
    assert paid.status_code == 303
    assert paid.headers["location"] == (
        f"{ORIGIN}/api/v1/payments/return?status=success&ref={api.order.reference}"
    )
    [webhook] = api.webhooks
    event = await api.mock.verify_webhook(dict(webhook.headers), webhook.content)
    assert event.event_type == CheckoutEventType.COMPLETED.value
    assert event.checkout_ref == payment["provider_ref"]
    session = await api.mock.get_checkout(str(payment["provider_ref"]))
    assert session.status is CheckoutStatus.COMPLETE

    # The bridge page, from the attempt's stored return URL: a button back to the app.
    bridge = await api.client.get(paid.headers["location"].removeprefix(ORIGIN))

    assert bridge.status_code == 200
    assert (
        f'href="livecart://payment-return?status=success&amp;reference={api.order.reference}"'
    ) in bridge.text


async def test_a_second_click_answers_the_same_url_from_the_recorded_attempt(
    api: CheckoutApi,
) -> None:
    await api.client.put(api.path("shipping-address"), json=ADDRESS, headers=api.bearer)

    first = await api.client.post(api.path("checkout"), json=RETURN, headers=api.bearer)
    api.clock.advance(timedelta(minutes=20))
    again = await api.client.post(api.path("checkout"), json=RETURN, headers=api.bearer)

    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(await api.rows("payments")) == 1


async def test_without_an_address_nothing_opens(api: CheckoutApi) -> None:
    refused = await api.client.post(api.path("checkout"), json=RETURN, headers=api.bearer)

    assert refused.status_code == 409
    assert await api.rows("payments") == []
