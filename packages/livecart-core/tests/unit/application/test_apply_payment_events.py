"""The payment events of the registry, applied by the worker's tick, over the in-memory fakes.

The events reach the registry as the webhook records them
(``ReceivePaymentWebhook``, signed by a fake mock provider); each tick of
``ProcessPaymentEvents`` then claims and applies them.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.apply_payment_events import (
    PAYMENT_EVENT_LEASE,
    ApplyPaymentEvent,
    HandledPaymentEvent,
    PaymentEventResult,
    PaymentEventsTick,
    ProcessPaymentEvents,
    effect_of,
)
from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    IntentSource,
    OrderRecord,
    PaymentEvent,
    PaymentProviderName,
    PaymentSettlement,
    RegisteredEvent,
    ReservationRequest,
    SessionEventType,
    SettlementKind,
    StockLevel,
    WebhookOutcome,
)
from livecart_core.application.payment_webhooks import ReceivePaymentWebhook
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.payments import PaymentEffect
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import (
    FakeClock,
    FakeEventPublisher,
    FakePaymentProvider,
    InMemoryDatabase,
    InMemoryWebhookEventRegistry,
)

TTL = timedelta(seconds=900)
BUDGET = timedelta(seconds=2)
AFTER_LEASE = PAYMENT_EVENT_LEASE + timedelta(seconds=1)


class Shop:
    """One order of 2 x B07 at 12.50 EUR with its attempt open, and the loop of the registry."""

    def __init__(self, registry: InMemoryWebhookEventRegistry | None = None) -> None:
        self.clock = FakeClock()
        self.database = InMemoryDatabase()
        self.registry = registry if registry is not None else self.database.webhook_events
        self.events = FakeEventPublisher()
        self.provider = FakePaymentProvider(PaymentProviderName.MOCK)
        self.webhook = ReceivePaymentWebhook(self.provider, self.registry, self.clock)
        self.session = self.database.add_live_session()
        self.product_id = self.database.add_product(
            self.session.id, "B07", price=Money(1_250, "EUR")
        ).id
        self.process = ProcessPaymentEvents(
            self.registry,
            ApplyPaymentEvent(self.registry, self.events, self.clock),
            self.clock,
            budget=BUDGET,
        )
        self.order_id = UUID(int=0)
        self.payment_id = UUID(int=0)

    async def open(self, comment: str = "c-1", ref: str = "mock_cs_1") -> None:
        envelope = queued_comment(external_comment_id=comment, author=f"buyer-of-{comment}")
        claim = await self.database.comments.claim(
            envelope, holder="worker-1/1-0", now=self.clock.now(), lease=timedelta(seconds=30)
        )
        result = await self.database.orders.reserve(
            ReservationRequest(
                comment_id=claim.comment.id,
                session_id=self.session.id,
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
        self.order_id = result.order.id
        opening = await self.database.payments.open_attempt(
            self.order_id,
            provider=PaymentProviderName.MOCK,
            return_url="livecart://payment-return",
            now=self.clock.now(),
            grace=timedelta(seconds=600),
            max_attempts=3,
        )
        assert opening.payment is not None
        self.payment_id = opening.payment.id
        await self.database.payments.record_checkout(
            self.payment_id, provider_ref=ref, redirect_url=f"https://pay.invalid/{ref}"
        )

    def event(
        self,
        event_id: str,
        event_type: CheckoutEventType = CheckoutEventType.COMPLETED,
        /,
        **fields: object,
    ) -> PaymentEvent:
        values: dict[str, object] = {
            "provider": PaymentProviderName.MOCK,
            "event_id": event_id,
            "event_type": event_type.value,
            "checkout_ref": "mock_cs_1",
            "payment_ref": "mock_pi_1",
            "payment_status": CheckoutPaymentStatus.PAID,
            "amount_minor": 2_500,
            "currency": "EUR",
            "occurred_at": self.clock.now(),
        }
        values.update(fields)
        return PaymentEvent.model_validate(values)

    async def deliver(self, event: PaymentEvent) -> bool:
        """The provider posts the event to the webhook: whether it was a redelivery."""
        headers, body = self.provider.signed_webhook(event)
        return (await self.webhook(headers, body)).redelivery

    @property
    def order(self) -> OrderRecord:
        return self.database.tables.orders[self.order_id]

    @property
    def attempt(self) -> PaymentStatus:
        return self.database.tables.payments[self.payment_id].status

    @property
    def stock(self) -> StockLevel:
        return self.database.tables.products[self.product_id].stock

    def registered(self, event: PaymentEvent) -> RegisteredEvent:
        return self.database.tables.webhook_events[(event.provider, event.event_id)]

    def published(self) -> list[tuple[SessionEventType, str | None]]:
        return [(e.type, str(e.data.get("status"))) for e in self.events.published]


@pytest.fixture
async def shop() -> Shop:
    opened = Shop()
    await opened.open()
    return opened


def _results(tick: PaymentEventsTick) -> list[PaymentEventResult]:
    return [handled.result for handled in tick.handled]


# --- The four events ---------------------------------------------------------------------


async def test_a_completed_session_pays_its_order_and_sells_its_units(shop: Shop) -> None:
    event = shop.event("evt_1")
    await shop.deliver(event)
    shop.clock.advance(2)

    tick = await shop.process()

    [handled] = tick.handled
    assert (handled.result, handled.paid, handled.paid_after_expiry) == (
        PaymentEventResult.APPLIED,
        True,
        False,
    )
    assert (shop.order.status, shop.order.paid_at) == (OrderStatus.PAID, shop.clock.now())
    assert shop.attempt is PaymentStatus.SUCCEEDED
    assert shop.stock == StockLevel(10, reserved=0, sold=2)
    registered = shop.registered(event)
    assert (registered.outcome, registered.attempts) == (WebhookOutcome.APPLIED, 1)
    # Published after the commit: the order as it stands, then its product's counters.
    assert [e.type for e in shop.events.published] == [
        SessionEventType.ORDER_UPDATED,
        SessionEventType.STOCK_UPDATED,
    ]
    assert shop.events.published[0].data["status"] == "paid"
    assert shop.events.published[1].data == {
        "product_id": str(shop.product_id),
        "stock_total": 10,
        "reserved": 0,
        "sold": 2,
        "available": 8,
    }


async def test_the_same_event_delivered_five_times_is_applied_once(shop: Shop) -> None:
    event = shop.event("evt_1")

    redeliveries = [await shop.deliver(event) for _ in range(5)]
    first = await shop.process()
    shop.clock.advance(AFTER_LEASE)
    second = await shop.process()

    assert redeliveries == [False, True, True, True, True]
    assert _results(first) == [PaymentEventResult.APPLIED]
    assert second.handled == ()
    registered = shop.registered(event)
    assert (registered.delivery_count, registered.attempts) == (5, 1)
    assert shop.stock == StockLevel(10, reserved=0, sold=2)
    assert len(shop.events.of_type(SessionEventType.ORDER_UPDATED)) == 1


async def test_events_out_of_order_change_nothing_they_should_not(shop: Shop) -> None:
    # A session completed unpaid (a delayed method), then paid, then its late
    # events, delivered newest first.
    unpaid = shop.event("evt_1", payment_status=CheckoutPaymentStatus.UNPAID)
    succeeded = shop.event("evt_2", CheckoutEventType.ASYNC_PAYMENT_SUCCEEDED)
    failed = shop.event(
        "evt_3", CheckoutEventType.ASYNC_PAYMENT_FAILED, amount_minor=None, currency=None
    )
    expired = shop.event("evt_4", CheckoutEventType.EXPIRED, amount_minor=None, currency=None)
    for event in (succeeded, expired, failed, unpaid, succeeded):
        await shop.deliver(event)
        shop.clock.advance(1)

    tick = await shop.process()

    assert _results(tick) == [
        PaymentEventResult.APPLIED,
        PaymentEventResult.STALE,
        PaymentEventResult.STALE,
        PaymentEventResult.NOT_ACTED_ON,
    ]
    assert (shop.order.status, shop.attempt) == (OrderStatus.PAID, PaymentStatus.SUCCEEDED)
    assert shop.stock == StockLevel(10, reserved=0, sold=2)
    assert [shop.registered(e).outcome for e in (succeeded, expired, failed, unpaid)] == [
        WebhookOutcome.APPLIED,
        WebhookOutcome.IGNORED,
        WebhookOutcome.IGNORED,
        WebhookOutcome.IGNORED,
    ]
    assert len(shop.events.of_type(SessionEventType.ORDER_UPDATED)) == 1


async def test_a_session_completed_unpaid_waits_for_its_async_payment(shop: Shop) -> None:
    await shop.deliver(shop.event("evt_1", payment_status=CheckoutPaymentStatus.UNPAID))
    waiting = await shop.process()
    await shop.deliver(shop.event("evt_2", CheckoutEventType.ASYNC_PAYMENT_SUCCEEDED))

    paid = await shop.process()

    assert _results(waiting) == [PaymentEventResult.NOT_ACTED_ON]
    assert _results(paid) == [PaymentEventResult.APPLIED]
    assert shop.order.status is OrderStatus.PAID


@pytest.mark.parametrize(
    ("event_type", "status"),
    [
        (CheckoutEventType.ASYNC_PAYMENT_FAILED, PaymentStatus.FAILED),
        (CheckoutEventType.EXPIRED, PaymentStatus.EXPIRED),
    ],
)
async def test_a_failed_or_expired_session_settles_its_attempt_and_keeps_the_stock(
    shop: Shop, event_type: CheckoutEventType, status: PaymentStatus
) -> None:
    await shop.deliver(shop.event("evt_1", event_type, amount_minor=None, currency=None))

    tick = await shop.process()

    [handled] = tick.handled
    assert (handled.result, handled.order_changed) == (PaymentEventResult.APPLIED, False)
    assert (shop.attempt, shop.order.status) == (status, OrderStatus.PENDING_PAYMENT)
    assert shop.stock == StockLevel(10, reserved=2)
    # The dashboard's order did not change: nothing to publish.
    assert shop.events.published == []


# --- What needs a person -----------------------------------------------------------------


@pytest.mark.parametrize(
    "charged",
    [{"amount_minor": 1}, {"currency": "USD"}, {"amount_minor": None, "currency": None}],
    ids=["amount", "currency", "none"],
)
async def test_a_payment_of_another_amount_is_failed_and_moves_nothing(
    shop: Shop, charged: dict[str, object]
) -> None:
    event = shop.event("evt_1", **charged)
    await shop.deliver(event)

    tick = await shop.process()

    [handled] = tick.handled
    assert handled.result is PaymentEventResult.AMOUNT_MISMATCH
    assert handled.result.outcome is WebhookOutcome.FAILED
    assert (handled.order_changed, handled.paid) == (False, False)
    assert handled.order == shop.order
    assert (shop.order.status, shop.attempt) == (OrderStatus.PENDING_PAYMENT, PaymentStatus.PENDING)
    assert shop.stock == StockLevel(10, reserved=2)
    assert shop.registered(event).outcome is WebhookOutcome.FAILED
    assert shop.events.published == []


async def test_a_payment_after_the_release_is_paid_after_expiry_without_stock(shop: Shop) -> None:
    # The attempt expired, then the sweep released the order: a payment
    # confirmed now, which expire-first rules out, is a defence in depth.
    await shop.database.payments.set_status(shop.payment_id, PaymentStatus.EXPIRED)
    shop.clock.advance(TTL + timedelta(seconds=1))
    await shop.database.orders.expire_due(now=shop.clock.now(), limit=10)
    assert shop.stock == StockLevel(10)
    await shop.deliver(shop.event("evt_late"))

    tick = await shop.process()

    [handled] = tick.handled
    assert (handled.result, handled.paid, handled.paid_after_expiry) == (
        PaymentEventResult.APPLIED,
        False,
        True,
    )
    assert shop.order.status is OrderStatus.PAID_AFTER_EXPIRY
    assert shop.stock == StockLevel(10)
    # The dashboard's badge: the order as it stands, and no stock event.
    assert shop.published() == [(SessionEventType.ORDER_UPDATED, "paid_after_expiry")]


async def test_an_event_of_no_attempt_is_ignored(shop: Shop) -> None:
    await shop.deliver(shop.event("evt_1", checkout_ref="cs_of_another_integration"))
    await shop.deliver(
        shop.event("evt_2", event_type=CheckoutEventType.COMPLETED, checkout_ref=None)
    )

    tick = await shop.process()

    assert _results(tick) == [PaymentEventResult.UNKNOWN_SESSION] * 2
    assert shop.order.status is OrderStatus.PENDING_PAYMENT


def test_only_the_four_checkout_session_events_have_an_effect() -> None:
    shop = Shop()

    assert effect_of(shop.event("e", CheckoutEventType.COMPLETED)) is PaymentEffect.PAID
    assert (
        effect_of(shop.event("e", CheckoutEventType.COMPLETED, payment_status=None))
        is PaymentEffect.PAID
    )
    assert (
        effect_of(
            shop.event("e", CheckoutEventType.COMPLETED, payment_status="no_payment_required")
        )
        is PaymentEffect.PAID
    )
    assert effect_of(shop.event("e", CheckoutEventType.ASYNC_PAYMENT_SUCCEEDED)) is (
        PaymentEffect.PAID
    )
    assert effect_of(shop.event("e", CheckoutEventType.ASYNC_PAYMENT_FAILED)) is (
        PaymentEffect.FAILED
    )
    assert effect_of(shop.event("e", CheckoutEventType.EXPIRED)) is PaymentEffect.EXPIRED
    other = shop.event("e").model_copy(update={"event_type": "payment_intent.created"})
    assert effect_of(other) is None


# --- Attempts ------------------------------------------------------------------------------


class FlakyRegistry(InMemoryWebhookEventRegistry):
    """A registry whose settlements fail ``failures`` times, as a database that keeps timing out."""

    def __init__(self, database: InMemoryDatabase, failures: int) -> None:
        super().__init__(database)
        self.failures = failures

    async def settle(
        self, event: PaymentEvent, effect: PaymentEffect, *, now: datetime
    ) -> PaymentSettlement | None:
        if self.failures:
            self.failures -= 1
            raise ServiceUnavailable("database unavailable: scripted")
        return await super().settle(event, effect, now=now)


async def _flaky_shop(failures: int) -> Shop:
    shop = Shop()
    shop.registry = FlakyRegistry(shop.database, failures)
    shop.webhook = ReceivePaymentWebhook(shop.provider, shop.registry, shop.clock)
    shop.process = ProcessPaymentEvents(
        shop.registry,
        ApplyPaymentEvent(shop.registry, shop.events, shop.clock),
        shop.clock,
        budget=BUDGET,
    )
    await shop.open()
    return shop


async def test_a_failed_attempt_is_tried_again_once_its_lease_has_ended() -> None:
    shop = await _flaky_shop(failures=2)
    event = shop.event("evt_1")
    await shop.deliver(event)

    ticks = [await shop.process()]
    shop.clock.advance(PAYMENT_EVENT_LEASE)
    ticks.append(await shop.process())
    for _ in range(2):
        shop.clock.advance(AFTER_LEASE)
        ticks.append(await shop.process())

    assert [_results(tick) for tick in ticks] == [
        [PaymentEventResult.RETRY],
        [],
        [PaymentEventResult.RETRY],
        [PaymentEventResult.APPLIED],
    ]
    retry = ticks[0].handled[0]
    assert isinstance(retry.error, ServiceUnavailable)
    assert shop.registered(event).attempts == 3
    assert shop.order.status is OrderStatus.PAID


async def test_an_event_fails_for_good_after_ten_attempts() -> None:
    shop = await _flaky_shop(failures=100)
    event = shop.event("evt_1")
    await shop.deliver(event)

    handled: list[HandledPaymentEvent] = []
    for _ in range(11):
        handled.extend((await shop.process()).handled)
        shop.clock.advance(AFTER_LEASE)

    assert [h.result for h in handled] == [PaymentEventResult.RETRY] * 9 + [
        PaymentEventResult.ATTEMPTS_EXHAUSTED
    ]
    registered = shop.registered(event)
    assert (registered.attempts, registered.outcome) == (10, WebhookOutcome.FAILED)
    assert isinstance(handled[-1].error, ServiceUnavailable)
    assert shop.order.status is OrderStatus.PENDING_PAYMENT


async def test_an_event_whose_worker_died_on_its_last_attempt_is_failed_by_the_next_claim(
    shop: Shop,
) -> None:
    event = shop.event("evt_1")
    await shop.deliver(event)
    # Ten claims, each of a worker that died before applying the event.
    for _ in range(10):
        await shop.registry.claim(
            now=shop.clock.now(), limit=20, lease=PAYMENT_EVENT_LEASE, max_attempts=10
        )
        shop.clock.advance(AFTER_LEASE)

    tick = await shop.process()

    assert _results(tick) == [PaymentEventResult.ATTEMPTS_EXHAUSTED]
    assert tick.handled[0].error is None
    assert shop.registered(event).outcome is WebhookOutcome.FAILED
    assert shop.order.status is OrderStatus.PENDING_PAYMENT


async def test_a_tick_claims_batch_after_batch_while_they_come_back_full() -> None:
    shop = Shop()
    await shop.open()
    for n in range(45):
        await shop.deliver(shop.event(f"evt_{n:02d}", CheckoutEventType.ASYNC_PAYMENT_FAILED))

    tick = await shop.process()

    assert (tick.claims, len(tick.handled)) == (3, 45)
    assert tick.count(PaymentEventResult.APPLIED) == 1
    assert tick.count(PaymentEventResult.REPLAYED) == 44


class SlowClock(FakeClock):
    """A clock that moves a second each time it is read, as a tick that takes its time."""

    def now(self) -> datetime:
        return self.advance(1)


async def test_a_tick_stops_claiming_once_its_budget_is_spent() -> None:
    shop = Shop()
    shop.clock = SlowClock()
    shop.process = ProcessPaymentEvents(
        shop.registry,
        ApplyPaymentEvent(shop.registry, shop.events, shop.clock),
        shop.clock,
        budget=BUDGET,
        batch=1,
    )
    await shop.open()
    for n in range(5):
        await shop.database.webhook_events.record(
            shop.event(f"evt_{n}", CheckoutEventType.ASYNC_PAYMENT_FAILED), now=shop.clock.now()
        )

    tick = await shop.process()

    assert tick.claims == 1
    assert len(tick.handled) == 1


async def test_an_event_another_claim_applied_first_is_left_alone(shop: Shop) -> None:
    event = shop.event("evt_1")
    await shop.deliver(event)
    [registered] = (
        await shop.registry.claim(
            now=shop.clock.now(), limit=20, lease=PAYMENT_EVENT_LEASE, max_attempts=10
        )
    ).claimed
    apply = ApplyPaymentEvent(shop.registry, shop.events, shop.clock)
    await apply(registered)

    again = await apply(registered)
    unacted = await apply(
        replace(registered, event=registered.event.model_copy(update={"event_type": "other"}))
    )

    assert (again.result, unacted.result) == (
        PaymentEventResult.PROCESSED_ELSEWHERE,
        PaymentEventResult.PROCESSED_ELSEWHERE,
    )
    assert again.result.outcome is None
    assert len(shop.events.of_type(SessionEventType.ORDER_UPDATED)) == 1


async def test_events_that_fail_to_publish_are_counted_and_the_payment_stands(
    shop: Shop,
) -> None:
    await shop.deliver(shop.event("evt_1"))
    shop.events.fail_next(2)

    tick = await shop.process()

    [handled] = tick.handled
    assert (handled.result, handled.unpublished_events) == (PaymentEventResult.APPLIED, 2)
    assert shop.order.status is OrderStatus.PAID


def test_a_tick_needs_a_budget_a_lease_a_batch_and_attempts() -> None:
    shop = Shop()
    apply = ApplyPaymentEvent(shop.registry, shop.events, shop.clock)
    registry, clock = shop.registry, shop.clock

    with pytest.raises(ValueError, match="time budget"):
        ProcessPaymentEvents(registry, apply, clock, budget=timedelta(0))
    with pytest.raises(ValueError, match="lease"):
        ProcessPaymentEvents(registry, apply, clock, budget=BUDGET, lease=timedelta(0))
    with pytest.raises(ValueError, match="one event at least"):
        ProcessPaymentEvents(registry, apply, clock, budget=BUDGET, batch=0)
    with pytest.raises(ValueError, match="one attempt at least"):
        ProcessPaymentEvents(registry, apply, clock, budget=BUDGET, max_attempts=0)


class DownMarks(FlakyRegistry):
    """Settlements that keep failing, and a registry that cannot mark the event failed either."""

    async def mark_processed(
        self,
        provider: PaymentProviderName,
        event_id: str,
        outcome: WebhookOutcome,
        *,
        now: datetime,
    ) -> bool:
        raise ServiceUnavailable("database unavailable: scripted")


async def test_a_last_attempt_that_cannot_mark_the_event_leaves_it_to_the_next_claim() -> None:
    shop = Shop()
    shop.registry = DownMarks(shop.database, failures=100)
    shop.webhook = ReceivePaymentWebhook(shop.provider, shop.registry, shop.clock)
    shop.process = ProcessPaymentEvents(
        shop.registry,
        ApplyPaymentEvent(shop.registry, shop.events, shop.clock),
        shop.clock,
        budget=BUDGET,
        max_attempts=1,
    )
    await shop.open()
    event = shop.event("evt_1")
    await shop.deliver(event)

    last = await shop.process()
    shop.clock.advance(AFTER_LEASE)
    next_claim = await shop.process()

    assert _results(last) == [PaymentEventResult.RETRY]
    assert _results(next_claim) == [PaymentEventResult.ATTEMPTS_EXHAUSTED]
    assert shop.registered(event).outcome is WebhookOutcome.FAILED


def test_a_settlement_changes_a_row_exactly_when_it_is_applied() -> None:
    with pytest.raises(ValueError, match="exactly when"):
        PaymentSettlement(SettlementKind.APPLIED)
    with pytest.raises(ValueError, match="exactly when"):
        PaymentSettlement(SettlementKind.REPLAYED, payment_changed=True)
    with pytest.raises(ValueError, match="carries it"):
        PaymentSettlement(SettlementKind.APPLIED, order_changed=True)
    assert SettlementKind.AMOUNT_MISMATCH.outcome is WebhookOutcome.FAILED
    assert SettlementKind.STALE.outcome is WebhookOutcome.IGNORED
