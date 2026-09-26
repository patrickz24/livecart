"""The buyer's address and checkout routes over the fakes: behind the order's token only.

``tests/integration/test_checkout_postgres.py`` runs the journey on
PostgreSQL, with the mock provider on Redis.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import timedelta

import httpx2
import pytest

from livecart_api.testing import make_app, make_settings, open_client, open_order
from livecart_core.application.dto import OrderRecord
from livecart_core.domain.money import Money
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus
from livecart_core.settings import AppEnv
from livecart_core.testing.fakes import (
    TEST_ORDER_TOKENS,
    FakeClock,
    FakePaymentProvider,
    InMemoryDatabase,
)

ORDERS = "/api/v1/orders"
NOT_FOUND = {"detail": "Not found"}
ADDRESS = {
    "full_name": "Ada Buyer",
    "line1": "1 rue du Live",
    "postal_code": "75001",
    "city": "Paris",
    "country": "FR",
}
RETURN = {"return_url": "livecart://payment-return"}
EXPO_GO = {"return_url": "exp://192.168.1.20:8081/--/payment-return"}


@dataclass
class Checkout:
    client: httpx2.AsyncClient
    database: InMemoryDatabase
    provider: FakePaymentProvider
    clock: FakeClock
    order: OrderRecord
    other: OrderRecord

    def bearer(self, order: OrderRecord | None = None) -> dict[str, str]:
        token = TEST_ORDER_TOKENS.token_for((order or self.order).id)
        return {"Authorization": f"Bearer {token}"}

    def path(self, action: str, order: OrderRecord | None = None) -> str:
        return f"{ORDERS}/{(order or self.order).reference}/{action}"

    async def address(self, order: OrderRecord | None = None) -> httpx2.Response:
        return await self.client.put(
            self.path("shipping-address", order), json=ADDRESS, headers=self.bearer(order)
        )

    async def checkout(self, body: dict[str, str] = RETURN) -> httpx2.Response:
        return await self.client.post(self.path("checkout"), json=body, headers=self.bearer())


async def _checkout(app_env: AppEnv = AppEnv.DEVELOPMENT) -> AsyncIterator[Checkout]:
    database = InMemoryDatabase()
    session = database.add_live_session()
    database.add_product(session.id, "B07", price=Money(1_250, "EUR"))
    order = await open_order(database, session.id, buyer="buyer-a", quantity=2)
    other = await open_order(database, session.id, buyer="buyer-b")
    # The fake's clock is the reservation's: the order was opened at its start.
    clock = FakeClock()
    provider = FakePaymentProvider()
    app = make_app(
        database=database,
        clock=clock,
        settings=make_settings(app_env),
        payment_provider=provider,
    )
    async with open_client(app) as client:
        yield Checkout(client, database, provider, clock, order, other)


@pytest.fixture
async def shop() -> AsyncIterator[Checkout]:
    async for checkout in _checkout():
        yield checkout


# --- The address -----------------------------------------------------------------------


async def test_the_address_is_recorded_behind_the_orders_token(shop: Checkout) -> None:
    response = await shop.address()
    details = await shop.client.get(shop.path("details"), headers=shop.bearer())

    assert response.status_code == 200
    assert response.json()["shipping_address"] == {**ADDRESS, "line2": ""}
    assert details.json()["shipping_address"] == response.json()["shipping_address"]
    assert "Ada Buyer" not in (await shop.client.get(f"{ORDERS}/{shop.order.reference}")).text


async def test_the_address_of_another_order_or_without_a_token_is_not_found(
    shop: Checkout,
) -> None:
    path = shop.path("shipping-address")

    answers = [
        await shop.client.put(path, json=ADDRESS, headers=shop.bearer(shop.other)),
        await shop.client.put(path, json=ADDRESS),
        await shop.client.put(f"{ORDERS}/LC-000000/shipping-address", json=ADDRESS),
    ]

    assert [(answer.status_code, answer.json()) for answer in answers] == [(404, NOT_FOUND)] * 3
    assert shop.database.tables.orders[shop.order.id].shipping_address is None


@pytest.mark.parametrize(
    "body",
    [
        {**ADDRESS, "country": "France"},
        {**ADDRESS, "full_name": "  "},
        {**ADDRESS, "line1": "x" * 201},
        {**ADDRESS, "phone": "0600000000"},
        {key: value for key, value in ADDRESS.items() if key != "city"},
    ],
)
async def test_an_address_pydantic_refuses_is_422(shop: Checkout, body: dict[str, str]) -> None:
    response = await shop.client.put(
        shop.path("shipping-address"), json=body, headers=shop.bearer()
    )

    assert response.status_code == 422
    assert shop.database.tables.orders[shop.order.id].shipping_address is None


async def test_a_paid_or_expired_order_keeps_its_address(shop: Checkout) -> None:
    shop.database.tables.orders[shop.order.id] = replace(shop.order, status=OrderStatus.EXPIRED)

    response = await shop.address()

    assert response.status_code == 409
    assert "no longer waits for its payment" in response.json()["detail"]


# --- The checkout ----------------------------------------------------------------------


async def test_a_checkout_answers_the_providers_page_and_extends_the_reservation(
    shop: Checkout,
) -> None:
    await shop.address()
    shop.clock.advance(timedelta(seconds=850))

    response = await shop.checkout()

    assert response.status_code == 200
    assert response.json() == {
        "redirect_url": "https://payments.invalid/checkout/fake_cs_1",
        "attempt_no": 1,
    }
    [request] = shop.provider.requests
    assert request.amount == Money(2_500, "EUR")
    assert request.success_url == (
        f"http://localhost:8080/api/v1/payments/return?status=success&ref={shop.order.reference}"
    )
    order = shop.database.tables.orders[shop.order.id]
    assert order.reserved_until == shop.clock.now() + timedelta(seconds=600)


async def test_a_second_click_answers_the_same_url(shop: Checkout) -> None:
    await shop.address()

    first = await shop.checkout()
    again = await shop.checkout()

    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(shop.provider.requests) == 1


async def test_a_fourth_attempt_is_refused(shop: Checkout) -> None:
    await shop.address()
    for attempt_no in (1, 2, 3):
        response = await shop.checkout()
        assert response.json()["attempt_no"] == attempt_no
        pending = await shop.database.payments.get_pending(shop.order.id)
        assert pending is not None
        await shop.database.payments.set_status(pending.id, PaymentStatus.EXPIRED)

    fourth = await shop.checkout()

    assert fourth.status_code == 409
    assert fourth.json() == {"detail": "The order has used all its payment attempts"}


async def test_a_checkout_needs_an_address_first(shop: Checkout) -> None:
    response = await shop.checkout()

    assert response.status_code == 409
    assert response.json() == {"detail": "The order needs a shipping address before its checkout"}
    assert shop.provider.requests == []


async def test_an_ended_reservation_is_no_longer_payable(shop: Checkout) -> None:
    await shop.address()
    shop.clock.advance(timedelta(seconds=900))

    response = await shop.checkout()

    assert response.status_code == 409
    assert "can no longer be paid" in response.json()["detail"]


async def test_the_token_of_another_order_is_not_found(shop: Checkout) -> None:
    await shop.address()
    await shop.address(shop.other)

    answers = [
        await shop.client.post(shop.path("checkout"), json=RETURN, headers=shop.bearer(shop.other)),
        await shop.client.post(shop.path("checkout"), json=RETURN),
    ]

    assert [(answer.status_code, answer.json()) for answer in answers] == [(404, NOT_FOUND)] * 2
    assert shop.database.tables.payments == {}


@pytest.mark.parametrize(
    "return_url",
    [
        "https://evil.example/phish",
        "otherapp://payment-return",
        "livecart://payment-return?token=x",
        "exp://93.184.216.34:8081/--/payment-return",
    ],
)
async def test_a_return_url_outside_the_allow_list_is_refused(
    shop: Checkout, return_url: str
) -> None:
    await shop.address()

    response = await shop.checkout({"return_url": return_url})

    assert response.status_code == 422
    assert response.json() == {
        "detail": (
            "return_url must be a link of the app (<scheme>://...), without a query or a fragment"
        )
    }
    assert shop.database.tables.payments == {}


async def test_expo_go_is_a_return_url_in_development_only(shop: Checkout) -> None:
    await shop.address()

    developed = await shop.checkout(EXPO_GO)
    async for production in _checkout(AppEnv.PRODUCTION):
        await production.address()
        refused = await production.checkout(EXPO_GO)
        accepted = await production.checkout(RETURN)

    assert developed.status_code == 200
    assert refused.status_code == 422
    assert accepted.status_code == 200


async def test_a_body_with_anything_else_is_refused(shop: Checkout) -> None:
    await shop.address()

    response = await shop.checkout({**RETURN, "amount_minor": "1"})

    assert response.status_code == 422


async def test_checkouts_past_five_a_minute_per_token_get_429(shop: Checkout) -> None:
    await shop.address()

    answers = [await shop.checkout() for _ in range(6)]
    other = await shop.client.post(
        shop.path("checkout", shop.other), json=RETURN, headers=shop.bearer(shop.other)
    )

    assert [answer.status_code for answer in answers] == [200] * 5 + [429]
    assert "Retry-After" in answers[-1].headers
    # Another token has a window of its own: the order has no address yet.
    assert other.status_code == 409


async def test_a_provider_down_answers_503_and_the_next_click_recovers(shop: Checkout) -> None:
    await shop.address()
    shop.provider.fail_next()

    down = await shop.checkout()
    recovered = await shop.checkout()

    assert down.status_code == 503
    assert down.headers["Retry-After"] == "1"
    assert recovered.status_code == 200
    assert recovered.json()["attempt_no"] == 1
