"""The storefront and the two tiers of an order on the SQL adapters and livecart_test.

The orders are opened by unit 3 itself (``SqlOrderRepository.reserve``),
which stores the SHA-256 of each order's HMAC token: the details route must
accept that token, and only for its own order.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_api.app import create_app
from livecart_api.testing import (
    current_time_clock,
    make_settings,
    open_client,
    postgres_resources,
)
from livecart_core.application.dto import IntentSource, OrderRecord, ReservationRequest
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.quantity import Quantity
from livecart_core.testing.backends import ORDER_TOKENS, PostgresBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START, LOCKED_ACCOUNT

pytestmark = pytest.mark.integration

NOW = FAKE_CLOCK_START
NOT_FOUND = {"detail": "Not found"}


@dataclass
class BuyerApi:
    client: httpx2.AsyncClient
    order_a: OrderRecord
    order_b: OrderRecord

    def bearer(self, order: OrderRecord) -> dict[str, str]:
        return {"Authorization": f"Bearer {ORDER_TOKENS.token_for(order.id)}"}


async def _reserve(backend: PostgresBackend, session_id: UUID, buyer: str) -> OrderRecord:
    comment_id = await backend.add_comment(f"comment-{buyer}", author=buyer)
    result = await backend.orders.reserve(
        ReservationRequest(
            comment_id=comment_id,
            session_id=session_id,
            product_code=ProductCode("B07"),
            variant=Variant(),
            quantity=Quantity(1),
            source=IntentSource.PARSER,
            confidence=Decimal("1.00"),
            evidence=("B07",),
            now=NOW,
            reserved_until=NOW + timedelta(seconds=900),
        )
    )
    assert result is not None
    assert result.order is not None
    return result.order


@pytest.fixture
async def api(
    clean_database: None, app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> AsyncIterator[BuyerApi]:
    backend = PostgresBackend(app_engine, migrator_engine)
    seller = await backend.sellers.add(
        email="demo@example.test",
        slug="demo-seller",
        display_name="Demo shop",
        password_hash=LOCKED_ACCOUNT,
        now=NOW,
    )
    session = await backend.add_live_session(seller_id=seller.id)
    await backend.add_product(session.id, "B07", name="Sac en cuir", stock_total=3)
    order_a = await _reserve(backend, session.id, "buyer-a")
    order_b = await _reserve(backend, session.id, "buyer-b")
    app = create_app(make_settings(), postgres_resources(app_engine, clock=current_time_clock()))
    async with open_client(app) as client:
        yield BuyerApi(client, order_a, order_b)


async def test_the_storefront_shows_the_live_session_and_what_is_left(api: BuyerApi) -> None:
    response = await api.client.get("/api/v1/storefronts/demo-seller")

    body = response.json()
    assert response.status_code == 200
    assert (body["slug"], body["display_name"]) == ("demo-seller", "Demo shop")
    assert body["live_session"]["products"] == [
        {
            "code": "B07",
            "variant": "",
            "name": "Sac en cuir",
            "price_minor": 1000,
            "currency": "EUR",
            "available": 1,
        }
    ]
    assert "demo@example.test" not in response.text
    missing = await api.client.get("/api/v1/storefronts/nobody")
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND)


async def test_the_reference_alone_shows_the_public_projection(api: BuyerApi) -> None:
    typed = api.order_a.reference.value.lower().replace("-", "")

    response = await api.client.get(f"/api/v1/orders/{typed}")

    assert response.status_code == 200
    assert set(response.json()) == {
        "reference",
        "status",
        "product",
        "quantity",
        "total_minor",
        "currency",
        "reserved_until",
    }
    assert response.json()["reference"] == api.order_a.reference.value
    assert "buyer-a" not in response.text


async def test_the_details_need_the_orders_own_token(api: BuyerApi) -> None:
    details_a = f"/api/v1/orders/{api.order_a.reference}/details"
    details_b = f"/api/v1/orders/{api.order_b.reference}/details"

    opened = await api.client.get(details_a, headers=api.bearer(api.order_a))
    missing = await api.client.get(details_a)
    wrong = await api.client.get(details_a, headers={"Authorization": "Bearer " + "A" * 43})
    crossed = await api.client.get(details_b, headers=api.bearer(api.order_a))

    assert opened.status_code == 200
    assert opened.json()["shipping_address"] is None
    assert [r.status_code for r in (missing, wrong, crossed)] == [404, 404, 404]
    assert missing.json() == wrong.json() == crossed.json() == NOT_FOUND
