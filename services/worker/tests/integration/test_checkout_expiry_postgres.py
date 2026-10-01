"""The later phases of the expiry sweep on the compose PostgreSQL, against the mock on Redis.

The SQL repositories through the worker's pool, and ``MockPaymentProvider``,
whose sessions live in Redis as the API and the worker share them. The
mock is wrapped to note each call it gets and how many connections were
checked out of the pool at that moment (``PoolProbe``): none, since a call
never falls inside a unit, and the sweep never holds more than one. The
clock is injected, so the tests move time rather than wait.
"""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.apply_payment_events import ApplyPaymentEvent
from livecart_core.application.checkout import CheckoutPages
from livecart_core.application.dto import (
    CheckoutRequest,
    CheckoutSession,
    CheckoutStatus,
    IntentSource,
    OrderRecord,
    PaymentProviderName,
    PaymentRecord,
    RegisteredEvent,
    ReservationRequest,
    StockLevel,
    WebhookOutcome,
)
from livecart_core.application.expire_checkouts import CheckoutExpiryOutcome, ExpireCheckouts
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus
from livecart_core.infrastructure.payments.mock import MockPaymentProvider
from livecart_core.testing.backends import PostgresBackend
from livecart_core.testing.fakes import FakeClock, FakeEventPublisher
from livecart_core.testing.keys import TEST_MOCK_PAYMENT_WEBHOOK_SECRET
from livecart_core.testing.pools import PoolProbe

pytestmark = pytest.mark.integration

TTL = timedelta(seconds=900)
GRACE = timedelta(seconds=600)
BUDGET = timedelta(seconds=5)
PAGES = CheckoutPages("http://localhost:8080")


class ObservedMock(MockPaymentProvider):
    """The mock, noting each call it gets and the connections checked out meanwhile."""

    def __init__(self, redis: Redis, *, prefix: str, clock: FakeClock) -> None:
        super().__init__(
            redis,
            prefix=prefix,
            secret=TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
            clock=clock,
            public_base_url=PAGES.public_base_url,
        )
        self.probe: PoolProbe | None = None
        self.calls: list[tuple[str, str]] = []
        self.checked_out: list[int] = []

    def _note(self, call: str, subject: str) -> None:
        self.calls.append((call, subject))
        if self.probe is not None:
            self.checked_out.append(self.probe.checked_out)

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        self._note("create", request.idempotency_key)
        return await super().create_checkout(request)

    async def expire_checkout(self, provider_ref: str) -> bool:
        self._note("expire", provider_ref)
        # A network call yields: the other sweep runs meanwhile.
        await asyncio.sleep(0.001)
        return await super().expire_checkout(provider_ref)

    async def get_checkout(self, provider_ref: str) -> CheckoutSession:
        self._note("get", provider_ref)
        return await super().get_checkout(provider_ref)


class Shop:
    """A live session selling B07 at 12.50 EUR on PostgreSQL, the mock, and the sweeps."""

    def __init__(
        self, backend: PostgresBackend, mock: ObservedMock, clock: FakeClock, engine: AsyncEngine
    ) -> None:
        self.backend, self.mock, self.clock, self.engine = backend, mock, clock, engine
        self.events = FakeEventPublisher()
        self.session_id = UUID(int=0)
        self.product_id = UUID(int=0)

    async def open(self) -> None:
        session = await self.backend.add_live_session()
        product = await self.backend.add_product(
            session.id, "B07", price=Money(1_250, "EUR"), stock_total=40
        )
        self.session_id, self.product_id = session.id, product.id

    def sweep(self, *, batch: int = 20) -> ExpireCheckouts:
        registry = self.backend.webhook_events
        return ExpireCheckouts(
            self.backend.orders,
            self.backend.payments,
            self.mock,
            registry,
            ApplyPaymentEvent(registry, self.events, self.clock),
            self.events,
            self.clock,
            pages=PAGES,
            budget=BUDGET,
            batch=batch,
        )

    async def order(self, comment: str) -> OrderRecord:
        """A pending order of 2 x B07, reserved until now + TTL."""
        comment_id = await self.backend.add_comment(comment, author=f"buyer-of-{comment}")
        result = await self.backend.orders.reserve(
            ReservationRequest(
                comment_id=comment_id,
                session_id=self.session_id,
                product_code=ProductCode("B07"),
                variant=Variant(),
                quantity=Quantity(2),
                source=IntentSource.PARSER,
                confidence=Decimal("1.00"),
                evidence=("B07",),
                now=self.clock.now(),
                reserved_until=self.clock.now() + TTL,
            )
        )
        assert result is not None
        assert result.order is not None
        return result.order

    async def attempt(self, order: OrderRecord, *, with_session: bool = True) -> PaymentRecord:
        """The order's attempt and, as a click makes it, its session at the mock, recorded."""
        opening = await self.backend.payments.open_attempt(
            order.id,
            provider=PaymentProviderName.MOCK,
            return_url="livecart://payment-return",
            now=self.clock.now(),
            grace=GRACE,
            max_attempts=3,
        )
        assert opening.payment is not None
        if not with_session:
            return opening.payment
        session = await MockPaymentProvider.create_checkout(
            self.mock, PAGES.request_for(order.reference, opening.payment)
        )
        return await self.backend.payments.record_checkout(
            opening.payment.id, provider_ref=session.provider_ref, redirect_url=session.redirect_url
        )

    async def stock(self) -> StockLevel:
        product = await self.backend.products.get(self.product_id)
        assert product is not None
        return product.stock

    async def status(self, order_id: UUID) -> tuple[OrderStatus, PaymentStatus]:
        order = await self.backend.orders.get(order_id)
        attempt = await self.backend.payments.latest(order_id)
        assert order is not None
        assert attempt is not None
        return order.status, attempt.status


@pytest.fixture
async def shop(
    postgres_backend: PostgresBackend,
    worker_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
) -> Shop:
    clock = FakeClock(datetime.now(UTC))
    mock = ObservedMock(redis_client, prefix=redis_key_prefix, clock=clock)
    opened = Shop(postgres_backend, mock, clock, worker_engine)
    await opened.open()
    return opened


async def test_a_session_paid_first_is_paid_with_no_connection_held_during_the_calls(
    shop: Shop,
) -> None:
    order = await shop.order("c-1")
    attempt = await shop.attempt(order)
    assert attempt.provider_ref is not None
    # The buyer paid on the hosted page; the webhook never came.
    await shop.mock.pay(attempt.provider_ref)
    shop.clock.advance(TTL + timedelta(seconds=1))

    with PoolProbe(shop.engine) as probe:
        shop.mock.probe = probe
        tick = await shop.sweep()()

    [handled] = tick.handled
    assert (handled.outcome, handled.paid) == (CheckoutExpiryOutcome.RECONCILED, True)
    assert shop.mock.calls == [("expire", attempt.provider_ref), ("get", attempt.provider_ref)]
    assert shop.mock.checked_out == [0, 0]
    assert probe.peak == 1
    assert await shop.status(order.id) == (OrderStatus.PAID, PaymentStatus.SUCCEEDED)
    assert await shop.stock() == StockLevel(40, reserved=0, sold=2)
    reconciled = await shop.backend.webhook_event(
        PaymentProviderName.MOCK, f"reconciled:{attempt.provider_ref}"
    )
    assert reconciled is not None
    assert reconciled.outcome is WebhookOutcome.APPLIED
    # The mock's own webhook, arriving late, is a replay of the same payment.
    hosted = await shop.mock.hosted_session(attempt.provider_ref)
    late = shop.mock.completed_event(hosted)
    await shop.backend.webhook_events.record(late, now=shop.clock.now())
    applied = await ApplyPaymentEvent(shop.backend.webhook_events, shop.events, shop.clock)(
        await _registered(shop, late.event_id)
    )
    assert applied.order_changed is False
    assert await shop.stock() == StockLevel(40, reserved=0, sold=2)


async def _registered(shop: Shop, event_id: str) -> RegisteredEvent:
    found = await shop.backend.webhook_event(PaymentProviderName.MOCK, event_id)
    assert found is not None
    return found


async def test_a_session_left_open_is_expired_at_the_mock_then_released(shop: Shop) -> None:
    order = await shop.order("c-1")
    attempt = await shop.attempt(order)
    assert attempt.provider_ref is not None
    shop.clock.advance(TTL + timedelta(seconds=1))

    with PoolProbe(shop.engine) as probe:
        shop.mock.probe = probe
        tick = await shop.sweep()()

    [handled] = tick.handled
    assert (handled.outcome, handled.released) == (CheckoutExpiryOutcome.EXPIRED, True)
    assert shop.mock.calls == [("expire", attempt.provider_ref)]
    assert shop.mock.checked_out == [0]
    assert probe.peak == 1
    assert await shop.status(order.id) == (OrderStatus.EXPIRED, PaymentStatus.EXPIRED)
    assert await shop.stock() == StockLevel(40)
    # The buyer's page is closed: paying now changes nothing.
    paid = await shop.mock.pay(attempt.provider_ref)
    assert paid.session.status is CheckoutStatus.EXPIRED


async def test_an_attempt_left_without_its_session_gets_the_same_one_then_expires(
    shop: Shop,
) -> None:
    order = await shop.order("c-1")
    clicked = await shop.attempt(order, with_session=False)
    # The click reached the mock, then crashed before recording its session.
    first = await MockPaymentProvider.create_checkout(
        shop.mock, PAGES.request_for(order.reference, clicked)
    )
    shop.clock.advance(TTL + timedelta(seconds=1))

    [handled] = (await shop.sweep()()).handled

    assert (handled.outcome, handled.recreated) == (CheckoutExpiryOutcome.EXPIRED, True)
    recorded = await shop.backend.payments.latest(order.id)
    assert recorded is not None
    assert (recorded.provider_ref, recorded.status) == (first.provider_ref, PaymentStatus.EXPIRED)
    assert shop.mock.calls == [
        ("create", f"checkout-session:{order.id}:1"),
        ("expire", first.provider_ref),
    ]
    assert await shop.stock() == StockLevel(40)


async def test_two_sweeps_at_once_expire_each_session_once_on_postgresql(shop: Shop) -> None:
    orders = [await shop.order(f"c-{n}") for n in range(20)]
    refs = {(await shop.attempt(order)).provider_ref for order in orders}
    shop.clock.advance(TTL + timedelta(seconds=1))

    ticks = await asyncio.gather(shop.sweep(batch=3)(), shop.sweep(batch=3)())

    handled = [checkout for tick in ticks for checkout in tick.handled]
    assert {checkout.outcome for checkout in handled} == {CheckoutExpiryOutcome.EXPIRED}
    assert sorted(checkout.due.order.id for checkout in handled) == sorted(
        order.id for order in orders
    )
    expired = Counter(ref for call, ref in shop.mock.calls if call == "expire")
    assert set(expired) == refs
    assert set(expired.values()) == {1}
    # Each sweep took some: they ran at once, not one after the other.
    assert all(tick.handled for tick in ticks)
    assert await shop.stock() == StockLevel(40)
    for order in orders:
        assert await shop.status(order.id) == (OrderStatus.EXPIRED, PaymentStatus.EXPIRED)
