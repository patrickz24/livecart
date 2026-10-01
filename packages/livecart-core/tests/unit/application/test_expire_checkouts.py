"""``ExpireCheckouts``, the later phases of the expiry sweep, over the in-memory fakes.

The orders open their attempts through ``StartCheckout``, as the buyer's
clicks do, so that every interleaving with a checkout is the real one. The
rules of the lease and of the release, on the fake and on PostgreSQL alike,
are in ``tests/contract/test_checkout_expiry.py``.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.apply_payment_events import (
    ApplyPaymentEvent,
    HandledPaymentEvent,
    PaymentEventResult,
    ProcessPaymentEvents,
)
from livecart_core.application.buyer_orders import LookUpOrder
from livecart_core.application.checkout import (
    CheckoutPages,
    CheckoutRefusal,
    CheckoutRefused,
    ReturnUrlPolicy,
    StartCheckout,
    StartedCheckout,
)
from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    CheckoutRequest,
    CheckoutSession,
    CheckoutStatus,
    IntentSource,
    OrderRecord,
    PaymentEvent,
    PaymentProviderName,
    PaymentRecord,
    RegisteredEvent,
    ReservationRequest,
    SessionEventType,
    ShippingAddress,
    StockLevel,
    WebhookOutcome,
)
from livecart_core.application.expire_checkouts import (
    LEASE_MARGIN,
    CheckoutExpiryOutcome,
    CheckoutExpiryTick,
    ExpireCheckouts,
    HandledCheckout,
    reconciliation_event,
)
from livecart_core.application.ports import (
    CHECKOUT_EXPIRY_BATCH,
    CHECKOUT_EXPIRY_LEASE,
    RecordNotFound,
    ServiceUnavailable,
)
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import (
    TEST_ORDER_TOKENS,
    FakeClock,
    FakeEventPublisher,
    FakePaymentProvider,
    InMemoryDatabase,
    InMemoryPaymentRepository,
)
from livecart_core.testing.polling import until

TTL = timedelta(seconds=900)
GRACE = timedelta(seconds=600)
BUDGET = timedelta(seconds=5)
AFTER_LEASE = CHECKOUT_EXPIRY_LEASE + timedelta(seconds=1)
BASE_URL = "http://localhost:8080"
RETURN_URL = "livecart://payment-return"
ADDRESS = ShippingAddress(
    full_name="Camille Martin",
    line1="12 rue des Lilas",
    postal_code="69003",
    city="Lyon",
    country="FR",
)


class Provider(FakePaymentProvider):
    """The fake provider, noting each session it is asked to expire, as a network call yields.

    ``expire_gate``, when set, holds every expiry until it opens;
    ``hold_next_create`` holds the next creation of a session until its event is set.
    """

    def __init__(self) -> None:
        super().__init__(PaymentProviderName.MOCK)
        self.expired: list[str] = []
        self.in_flight = 0
        self.most_in_flight = 0
        self.expire_gate: asyncio.Event | None = None
        self.create_hold: asyncio.Event | None = None
        self.creates_waiting = 0

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        hold, self.create_hold = self.create_hold, None
        if hold is not None:
            self.creates_waiting += 1
            await hold.wait()
            self.creates_waiting -= 1
        return await super().create_checkout(request)

    async def expire_checkout(self, provider_ref: str) -> bool:
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            self.expired.append(provider_ref)
            await asyncio.sleep(0)
            if self.expire_gate is not None:
                await self.expire_gate.wait()
            return await super().expire_checkout(provider_ref)
        finally:
            self.in_flight -= 1


class Shop:
    """A live session selling B07 at 12.50 EUR, its buyers' checkouts, and the sweep."""

    def __init__(
        self,
        *,
        provider: Provider | None = None,
        payments: type[InMemoryPaymentRepository] = InMemoryPaymentRepository,
    ) -> None:
        self.clock = FakeClock()
        self.database = InMemoryDatabase()
        self.events = FakeEventPublisher()
        self.provider = provider if provider is not None else Provider()
        self.payments = payments(self.database)
        session = self.database.add_live_session()
        self.product_id = self.database.add_product(session.id, "B07", price=Money(1_250, "EUR")).id
        self.session_id = session.id
        self.registry = self.database.webhook_events
        self.apply = ApplyPaymentEvent(self.registry, self.events, self.clock)
        self.pages = CheckoutPages(BASE_URL)
        self.checkout = StartCheckout(
            LookUpOrder(self.database.orders, TEST_ORDER_TOKENS.verify),
            self.payments,
            self.provider,
            self.clock,
            pages=self.pages,
            return_urls=ReturnUrlPolicy(frozenset({"livecart"}), allow_expo_go=False),
            grace=GRACE,
            max_attempts=3,
        )
        self.sweep = self.sweeper()

    def sweeper(
        self,
        *,
        apply: ApplyPaymentEvent | None = None,
        batch: int = CHECKOUT_EXPIRY_BATCH,
        lease: timedelta = CHECKOUT_EXPIRY_LEASE,
    ) -> ExpireCheckouts:
        return ExpireCheckouts(
            self.database.orders,
            self.payments,
            self.provider,
            self.registry,
            apply if apply is not None else self.apply,
            self.events,
            self.clock,
            pages=self.pages,
            budget=BUDGET,
            batch=batch,
            lease=lease,
        )

    async def order(self, buyer: str = "buyer-1") -> OrderRecord:
        """A pending order of 2 x B07, reserved until now + TTL, with its address."""
        claim = await self.database.comments.claim(
            queued_comment(external_comment_id=f"comment-{buyer}", author=buyer),
            holder="worker-1/1-0",
            now=self.clock.now(),
            lease=timedelta(seconds=30),
        )
        result = await self.database.orders.reserve(
            ReservationRequest(
                comment_id=claim.comment.id,
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
        return await self.database.orders.set_shipping_address(result.order.id, ADDRESS)

    async def click(self, order: OrderRecord) -> StartedCheckout:
        """The buyer's click on Pay: the hosted page of the order's attempt."""
        started = await self.checkout.start(
            order.reference.value, TEST_ORDER_TOKENS.token_for(order.id), RETURN_URL
        )
        assert started is not None
        return started

    async def paying(self, buyer: str = "buyer-1") -> tuple[OrderRecord, PaymentRecord]:
        """An order whose buyer opened the hosted page, then left it."""
        order = await self.order(buyer)
        await self.click(order)
        return order, self.attempt(order.id)

    def attempt(self, order_id: UUID) -> PaymentRecord:
        [attempt] = [
            row for row in self.database.tables.payments.values() if row.order_id == order_id
        ]
        return attempt

    def stored(self, order_id: UUID) -> OrderRecord:
        return self.database.tables.orders[order_id]

    def stock(self) -> StockLevel:
        return self.database.tables.products[self.product_id].stock

    def past_reservations(self) -> None:
        self.clock.advance(TTL + timedelta(seconds=1))


def _only(tick: CheckoutExpiryTick) -> HandledCheckout:
    [handled] = tick.handled
    return handled


# --- Expired first ---------------------------------------------------------------------


async def test_a_session_the_sweep_expires_releases_the_order_and_its_stock() -> None:
    shop = Shop()
    order, attempt = await shop.paying()
    shop.past_reservations()

    tick = await shop.sweep()

    handled = _only(tick)
    assert (handled.outcome, handled.released, handled.paid) == (
        CheckoutExpiryOutcome.EXPIRED,
        True,
        False,
    )
    assert handled.order == shop.stored(order.id)
    assert handled.order.status is OrderStatus.EXPIRED
    assert (handled.recreated, handled.error, tick.claims) == (False, None, 1)
    assert shop.provider.expired == [attempt.provider_ref]
    assert (await shop.provider.get_checkout("fake_cs_1")).status is CheckoutStatus.EXPIRED
    assert shop.attempt(order.id).status is PaymentStatus.EXPIRED
    assert shop.stock() == StockLevel(10)
    assert [event.type for event in shop.events.published] == [
        SessionEventType.ORDER_UPDATED,
        SessionEventType.STOCK_UPDATED,
    ]
    assert shop.events.published[0].data["status"] == "expired"
    # Nothing is due any more.
    assert await shop.sweep() == CheckoutExpiryTick(claims=1)


async def test_a_session_found_expired_releases_the_order_the_same_way() -> None:
    shop = Shop()
    order, attempt = await shop.paying()
    assert attempt.provider_ref is not None
    # Expired at the provider already: by an earlier sweep that crashed before
    # its release, or by the provider itself.
    await FakePaymentProvider.expire_checkout(shop.provider, attempt.provider_ref)
    shop.past_reservations()

    handled = _only(await shop.sweep())

    assert (handled.outcome, handled.released) == (CheckoutExpiryOutcome.FOUND_EXPIRED, True)
    assert shop.stored(order.id).status is OrderStatus.EXPIRED
    assert shop.stock() == StockLevel(10)


async def test_a_buyer_who_clicks_after_the_release_is_refused() -> None:
    shop = Shop()
    order, _ = await shop.paying()
    shop.past_reservations()
    await shop.sweep()

    with pytest.raises(CheckoutRefused) as refused:
        await shop.click(order)

    assert refused.value.reason is CheckoutRefusal.NOT_PAYABLE
    assert len(shop.database.tables.payments) == 1


async def test_events_that_fail_to_publish_are_counted() -> None:
    shop = Shop()
    await shop.paying()
    shop.past_reservations()
    shop.events.fail_next(1)

    handled = _only(await shop.sweep())

    assert (handled.released, handled.unpublished_events) == (True, 1)


# --- Paid first ------------------------------------------------------------------------


async def test_a_session_the_buyer_paid_first_is_paid_without_releasing_its_stock() -> None:
    shop = Shop()
    order, attempt = await shop.paying()
    assert attempt.provider_ref is not None
    # The buyer paid, and the provider's webhook has not arrived.
    shop.provider.complete(attempt.provider_ref, payment_ref="fake_pi_1")
    shop.past_reservations()

    handled = _only(await shop.sweep())

    assert (handled.outcome, handled.paid, handled.released) == (
        CheckoutExpiryOutcome.RECONCILED,
        True,
        False,
    )
    assert shop.stored(order.id).status is OrderStatus.PAID
    assert shop.stock() == StockLevel(10, reserved=0, sold=2)
    paid = shop.attempt(order.id)
    assert (paid.status, paid.provider_payment_ref) == (PaymentStatus.SUCCEEDED, "fake_pi_1")
    reconciled = shop.database.tables.webhook_events[
        (PaymentProviderName.MOCK, f"reconciled:{attempt.provider_ref}")
    ]
    assert reconciled.outcome is WebhookOutcome.APPLIED
    assert reconciled.event.event_type == CheckoutEventType.COMPLETED.value
    assert [event.type for event in shop.events.published] == [
        SessionEventType.ORDER_UPDATED,
        SessionEventType.STOCK_UPDATED,
    ]
    assert shop.events.published[0].data["status"] == "paid"


async def test_the_providers_own_webhook_after_a_reconciliation_changes_nothing() -> None:
    shop = Shop()
    order, attempt = await shop.paying()
    assert attempt.provider_ref is not None
    shop.provider.complete(attempt.provider_ref, payment_ref="fake_pi_1")
    shop.past_reservations()
    await shop.sweep()
    webhook = PaymentEvent(
        provider=PaymentProviderName.MOCK,
        event_id="evt_completed",
        event_type=CheckoutEventType.COMPLETED.value,
        checkout_ref=attempt.provider_ref,
        payment_ref="fake_pi_1",
        payment_status=CheckoutPaymentStatus.PAID,
        amount_minor=2_500,
        currency="EUR",
        occurred_at=shop.clock.now(),
    )
    await shop.registry.record(webhook, now=shop.clock.now())

    tick = await ProcessPaymentEvents(shop.registry, shop.apply, shop.clock, budget=BUDGET)()

    assert [handled.result for handled in tick.handled] == [PaymentEventResult.REPLAYED]
    assert shop.stored(order.id).status is OrderStatus.PAID
    assert shop.stock() == StockLevel(10, reserved=0, sold=2)


class Unavailable(ApplyPaymentEvent):
    """An application of the registry's events while PostgreSQL is down."""

    async def __call__(self, registered: RegisteredEvent) -> HandledPaymentEvent:
        raise ServiceUnavailable("database unavailable: scripted")


async def test_a_reconciliation_the_registry_cannot_apply_now_is_left_to_its_loop() -> None:
    shop = Shop()
    order, attempt = await shop.paying()
    assert attempt.provider_ref is not None
    shop.provider.complete(attempt.provider_ref)
    shop.past_reservations()

    sweep = shop.sweeper(apply=Unavailable(shop.registry, shop.events, shop.clock))
    handled = _only(await sweep())

    assert handled.outcome is CheckoutExpiryOutcome.RECONCILED
    assert isinstance(handled.error, ServiceUnavailable)
    # Recorded, unprocessed: the registry's loop applies it.
    tick = await ProcessPaymentEvents(shop.registry, shop.apply, shop.clock, budget=BUDGET)()
    assert [item.result for item in tick.handled] == [PaymentEventResult.APPLIED]
    assert shop.stored(order.id).status is OrderStatus.PAID


# --- Not settled now -------------------------------------------------------------------


async def test_a_provider_down_leaves_the_order_to_the_sweep_after_the_lease() -> None:
    shop = Shop()
    order, _ = await shop.paying()
    shop.past_reservations()
    shop.provider.fail_next(1)

    first = _only(await shop.sweep())
    during_lease = await shop.sweep()
    shop.clock.advance(AFTER_LEASE)
    after_lease = _only(await shop.sweep())

    assert first.outcome is CheckoutExpiryOutcome.RETRY
    assert isinstance(first.error, ServiceUnavailable)
    assert during_lease == CheckoutExpiryTick(claims=1)
    assert after_lease.outcome is CheckoutExpiryOutcome.EXPIRED
    assert shop.stored(order.id).status is OrderStatus.EXPIRED
    assert shop.stock() == StockLevel(10)


class StillOpen(Provider):
    """A provider that refuses to expire an open session, and leaves it open."""

    async def expire_checkout(self, provider_ref: str) -> bool:
        self.expired.append(provider_ref)
        return False


class PaidLater(Provider):
    """A provider whose sessions complete by a delayed method: the money has not come in."""

    async def get_checkout(self, provider_ref: str) -> CheckoutSession:
        session = await super().get_checkout(provider_ref)
        return replace(
            session, status=CheckoutStatus.COMPLETE, payment_status=CheckoutPaymentStatus.UNPAID
        )

    async def expire_checkout(self, provider_ref: str) -> bool:
        self.expired.append(provider_ref)
        return False


@pytest.mark.parametrize("provider", [StillOpen, PaidLater])
async def test_a_session_neither_expired_nor_paid_waits_for_the_next_lease(
    provider: type[Provider],
) -> None:
    shop = Shop(provider=provider())
    order, _ = await shop.paying()
    shop.past_reservations()

    handled = _only(await shop.sweep())

    assert (handled.outcome, handled.error) == (CheckoutExpiryOutcome.RETRY, None)
    assert shop.stored(order.id).status is OrderStatus.PENDING_PAYMENT
    assert shop.attempt(order.id).status is PaymentStatus.PENDING
    assert shop.stock() == StockLevel(10, reserved=2)
    assert shop.database.tables.webhook_events == {}


async def test_a_claim_ends_before_its_lease_does() -> None:
    shop = Shop()
    await shop.paying()
    shop.past_reservations()
    shop.provider.expire_gate = asyncio.Event()  # never opens
    sweep = shop.sweeper(lease=LEASE_MARGIN + timedelta(milliseconds=200))

    handled = _only(await asyncio.wait_for(sweep(), timeout=5))

    assert handled.outcome is CheckoutExpiryOutcome.RETRY
    assert isinstance(handled.error, TimeoutError)
    assert shop.stock() == StockLevel(10, reserved=2)


# --- Needs a person --------------------------------------------------------------------


async def test_a_session_the_provider_does_not_know_fails() -> None:
    shop = Shop()
    order = await shop.order()
    opening = await shop.database.payments.open_attempt(
        order.id,
        provider=PaymentProviderName.MOCK,
        return_url=RETURN_URL,
        now=shop.clock.now(),
        grace=GRACE,
        max_attempts=3,
    )
    assert opening.payment is not None
    await shop.database.payments.record_checkout(
        opening.payment.id, provider_ref="cs_unknown", redirect_url="https://pay.invalid/x"
    )
    shop.past_reservations()

    handled = _only(await shop.sweep())

    assert handled.outcome is CheckoutExpiryOutcome.FAILED
    assert isinstance(handled.error, RecordNotFound)
    assert shop.stored(order.id).status is OrderStatus.PENDING_PAYMENT


async def test_an_attempt_of_another_provider_fails_untouched() -> None:
    shop = Shop()
    order = await shop.order()
    await shop.database.payments.open_attempt(
        order.id,
        provider=PaymentProviderName.STRIPE,
        return_url=RETURN_URL,
        now=shop.clock.now(),
        grace=GRACE,
        max_attempts=3,
    )
    shop.past_reservations()

    handled = _only(await shop.sweep())

    assert handled.outcome is CheckoutExpiryOutcome.FAILED
    assert isinstance(handled.error, ValueError)
    assert shop.provider.requests == []
    assert shop.provider.expired == []


# --- An attempt left without its session -----------------------------------------------


class LostRecord(InMemoryPaymentRepository):
    """The repository of a crash between the provider's answer and its record."""

    lose_next = True

    async def record_checkout(
        self, payment_id: UUID, *, provider_ref: str, redirect_url: str
    ) -> PaymentRecord:
        if self.lose_next:
            self.lose_next = False
            raise ServiceUnavailable("database unavailable: scripted")
        return await super().record_checkout(
            payment_id, provider_ref=provider_ref, redirect_url=redirect_url
        )


@pytest.mark.parametrize("crash", ["provider_down", "record_lost"])
async def test_an_attempt_left_without_its_session_gets_it_again_then_expires(crash: str) -> None:
    provider = Provider()
    shop = Shop(
        provider=provider,
        payments=LostRecord if crash == "record_lost" else InMemoryPaymentRepository,
    )
    order = await shop.order()
    if crash == "provider_down":
        provider.fail_next(1)
    with pytest.raises(ServiceUnavailable):
        await shop.click(order)
    assert shop.attempt(order.id).provider_ref is None
    shop.past_reservations()

    handled = _only(await shop.sweep())

    assert (handled.outcome, handled.recreated, handled.released) == (
        CheckoutExpiryOutcome.EXPIRED,
        True,
        True,
    )
    # The same request as the click's, under the same key: one session.
    requests = provider.requests
    assert requests[-1] == requests[0]
    assert requests[-1].idempotency_key == f"checkout-session:{order.id}:1"
    assert shop.attempt(order.id).provider_ref == "fake_cs_1"
    assert provider.expired == ["fake_cs_1"]
    assert (await provider.get_checkout("fake_cs_1")).status is CheckoutStatus.EXPIRED
    assert shop.stock() == StockLevel(10)


async def test_a_click_while_the_sweep_creates_the_session_again_gets_the_same_one() -> None:
    shop = Shop()
    order = await shop.order()
    shop.provider.fail_next(1)
    with pytest.raises(ServiceUnavailable):
        await shop.click(order)
    shop.past_reservations()
    hold = shop.provider.create_hold = asyncio.Event()

    sweeping = asyncio.create_task(shop.sweep())
    await until(lambda: shop.provider.creates_waiting == 1)
    # Meanwhile the buyer clicks again: the attempt is pending, its session missing.
    started = await shop.click(order)
    hold.set()
    handled = _only(await sweeping)

    assert started.redirect_url == "https://payments.invalid/checkout/fake_cs_1"
    assert {request.idempotency_key for request in shop.provider.requests} == {
        f"checkout-session:{order.id}:1"
    }
    assert shop.attempt(order.id).provider_ref == "fake_cs_1"
    assert (handled.outcome, handled.recreated) == (CheckoutExpiryOutcome.EXPIRED, True)
    # The page the click opened is expired: its buyer can no longer pay.
    with pytest.raises(ValueError, match="not open"):
        shop.provider.complete("fake_cs_1")
    assert shop.stock() == StockLevel(10)


# --- The checkout's grace, and several sweeps ------------------------------------------


async def test_a_checkout_that_extended_the_reservation_is_left_until_its_grace_ends() -> None:
    shop = Shop()
    order = await shop.order()
    shop.clock.advance(TTL - timedelta(seconds=30))
    await shop.click(order)
    extended = shop.stored(order.id).reserved_until
    shop.clock.advance(timedelta(seconds=31))

    before = await shop.sweep()
    swept = await shop.database.orders.expire_due(now=shop.clock.now(), limit=10)
    shop.clock.set(extended + timedelta(seconds=1))
    after = _only(await shop.sweep())

    # Past its first reservation, within its grace: neither phase takes it.
    assert before == CheckoutExpiryTick(claims=1)
    assert swept.orders == ()
    assert after.outcome is CheckoutExpiryOutcome.EXPIRED
    assert shop.stock() == StockLevel(10)


async def test_two_sweeps_at_once_make_one_expiry_call_per_order() -> None:
    shop = Shop()
    orders = [(await shop.paying(f"buyer-{n}"))[0] for n in range(5)]
    shop.past_reservations()
    first, second = shop.sweeper(batch=2), shop.sweeper(batch=2)

    ticks = await asyncio.gather(first(), second())

    handled = [checkout for tick in ticks for checkout in tick.handled]
    assert sorted(checkout.due.order.id for checkout in handled) == [order.id for order in orders]
    assert {checkout.outcome for checkout in handled} == {CheckoutExpiryOutcome.EXPIRED}
    assert sorted(shop.provider.expired) == sorted(f"fake_cs_{n}" for n in range(1, 6))
    assert shop.stock() == StockLevel(10)


async def test_the_provider_is_asked_four_at_a_time() -> None:
    shop = Shop()
    shop.database.tables.products[shop.product_id] = replace(
        shop.database.tables.products[shop.product_id], stock=StockLevel(40)
    )
    for n in range(10):
        await shop.paying(f"buyer-{n}")
    shop.past_reservations()
    gate = shop.provider.expire_gate = asyncio.Event()

    sweeping = asyncio.create_task(shop.sweep())
    await until(lambda: shop.provider.in_flight == 4)
    await asyncio.sleep(0.01)
    in_flight = shop.provider.in_flight
    gate.set()
    tick = await sweeping

    assert in_flight == 4
    assert shop.provider.most_in_flight == 4
    assert tick.count(CheckoutExpiryOutcome.EXPIRED) == 10
    assert shop.stock() == StockLevel(40)


# --- The records -----------------------------------------------------------------------


def test_the_reconciliation_is_the_providers_completed_event_under_an_id_of_its_own() -> None:
    clock = FakeClock()
    session = CheckoutSession(
        "cs_1",
        "https://pay.invalid/cs_1",
        CheckoutStatus.COMPLETE,
        Money(2_500, "EUR"),
        payment_ref="pi_1",
        payment_status=CheckoutPaymentStatus.PAID,
    )

    event = reconciliation_event(PaymentProviderName.STRIPE, session, now=clock.now())

    assert event == PaymentEvent(
        provider=PaymentProviderName.STRIPE,
        event_id="reconciled:cs_1",
        event_type="checkout.session.completed",
        checkout_ref="cs_1",
        payment_ref="pi_1",
        payment_status=CheckoutPaymentStatus.PAID,
        amount_minor=2_500,
        currency="EUR",
        occurred_at=clock.now(),
    )


@pytest.mark.parametrize(
    ("budget", "lease", "batch", "slots"),
    [
        (timedelta(0), CHECKOUT_EXPIRY_LEASE, 20, 4),
        (BUDGET, LEASE_MARGIN, 20, 4),
        (BUDGET, CHECKOUT_EXPIRY_LEASE, 0, 4),
        (BUDGET, CHECKOUT_EXPIRY_LEASE, 20, 0),
    ],
)
def test_a_sweep_needs_a_budget_a_lease_longer_than_its_margin_a_batch_and_slots(
    budget: timedelta, lease: timedelta, batch: int, slots: int
) -> None:
    shop = Shop()

    with pytest.raises(ValueError, match=r"needs|leases"):
        ExpireCheckouts(
            shop.database.orders,
            shop.payments,
            shop.provider,
            shop.registry,
            shop.apply,
            shop.events,
            shop.clock,
            pages=shop.pages,
            budget=budget,
            lease=lease,
            batch=batch,
            slots=slots,
        )
