"""The registry of payment webhooks on every backend: the in-memory fake, then PostgreSQL.

One row per (provider, event_id), whatever the deliveries: the first is
recorded with the event's allow-listed payload, each later one only counted.
Claims take the oldest unprocessed events whose lease has ended, disjoint
when they run at once, each counting an attempt, and fail an event out of
attempts. A settlement applies one event to its attempt, its order and its
stock in one unit, once, and marks it processed with its outcome.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    EventClaim,
    IntentSource,
    OrderRecord,
    PaymentEvent,
    PaymentProviderName,
    PaymentRecord,
    PaymentSettlement,
    RegisteredEvent,
    ReservationRequest,
    SettlementKind,
    StockLevel,
    WebhookOutcome,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.payments import PaymentEffect
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus
from livecart_core.testing.backends import ReservationBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
LEASE = timedelta(seconds=30)
EVENT = PaymentEvent(
    provider=PaymentProviderName.MOCK,
    event_id="evt_1",
    event_type=CheckoutEventType.COMPLETED.value,
    checkout_ref="mock_cs_1",
    payment_ref="mock_pi_1",
    payment_status=CheckoutPaymentStatus.PAID,
    amount_minor=2_500,
    currency="EUR",
    occurred_at=NOW - timedelta(seconds=3),
)


def _event(event_id: str, /, **fields: object) -> PaymentEvent:
    return EVENT.model_copy(update={"event_id": event_id, **fields})


# --- Recording -------------------------------------------------------------------------


async def test_an_event_is_recorded_once_with_its_payload(backend: ReservationBackend) -> None:
    assert await backend.webhook_events.record(EVENT, now=NOW) is True

    registered = await backend.webhook_event(EVENT.provider, EVENT.event_id)
    assert registered is not None
    assert registered.event == EVENT
    assert registered.received_at == NOW
    assert (registered.delivery_count, registered.attempts) == (1, 0)
    assert (registered.claimed_until, registered.processed_at, registered.outcome) == (
        None,
        None,
        None,
    )


async def test_a_redelivery_is_counted_and_keeps_the_first_payload(
    backend: ReservationBackend,
) -> None:
    await backend.webhook_events.record(EVENT, now=NOW)
    # The same id with other fields: only the first delivery's payload counts.
    altered = EVENT.model_copy(update={"amount_minor": 1, "event_type": "altered"})

    again = [
        await backend.webhook_events.record(event, now=NOW + timedelta(seconds=n))
        for n, event in enumerate((EVENT, altered, EVENT, EVENT), start=1)
    ]

    registered = await backend.webhook_event(EVENT.provider, EVENT.event_id)
    assert again == [False] * 4
    assert registered is not None
    assert (registered.event, registered.received_at) == (EVENT, NOW)
    assert (registered.delivery_count, registered.attempts, registered.outcome) == (5, 0, None)


async def test_an_event_id_names_one_event_per_provider(backend: ReservationBackend) -> None:
    stripe = EVENT.model_copy(update={"provider": PaymentProviderName.STRIPE})

    assert await backend.webhook_events.record(EVENT, now=NOW) is True
    assert await backend.webhook_events.record(stripe, now=NOW) is True

    for provider in (PaymentProviderName.MOCK, PaymentProviderName.STRIPE):
        registered = await backend.webhook_event(provider, EVENT.event_id)
        assert registered is not None
        assert (registered.event.provider, registered.delivery_count) == (provider, 1)
    assert await backend.webhook_event(PaymentProviderName.MOCK, "evt_404") is None


async def test_deliveries_racing_each_other_end_on_one_row(backend: ReservationBackend) -> None:
    firsts = await asyncio.gather(
        *(backend.webhook_events.record(EVENT, now=NOW) for _ in range(10))
    )

    registered = await backend.webhook_event(EVENT.provider, EVENT.event_id)
    assert firsts.count(True) == 1
    assert registered is not None
    assert registered.delivery_count == 10


# --- Claims ----------------------------------------------------------------------------


async def _claim(
    backend: ReservationBackend,
    at: timedelta = timedelta(0),
    *,
    limit: int = 20,
    max_attempts: int = 10,
) -> EventClaim:
    return await backend.webhook_events.claim(
        now=NOW + at, limit=limit, lease=LEASE, max_attempts=max_attempts
    )


def _ids(events: tuple[RegisteredEvent, ...]) -> list[str]:
    return [registered.event.event_id for registered in events]


async def test_a_claim_takes_the_oldest_unprocessed_events_and_counts_an_attempt(
    backend: ReservationBackend,
) -> None:
    for n, event_id in enumerate(("evt_b", "evt_a", "evt_c")):
        await backend.webhook_events.record(_event(event_id), now=NOW + timedelta(seconds=n))
    at = timedelta(seconds=5)

    first = await _claim(backend, at, limit=2)
    second = await _claim(backend, at, limit=2)

    assert _ids(first.claimed) == ["evt_b", "evt_a"]
    assert [(e.attempts, e.claimed_until) for e in first.claimed] == [(1, NOW + at + LEASE)] * 2
    # The first two are leased: the next claim takes the third only.
    assert _ids(second.claimed) == ["evt_c"]
    assert first.exhausted == second.exhausted == ()
    stored = await backend.webhook_event(EVENT.provider, "evt_b")
    assert stored is not None
    assert (stored.attempts, stored.claimed_until, stored.processed_at) == (
        1,
        NOW + at + LEASE,
        None,
    )


async def test_a_claimed_event_is_claimed_again_once_its_lease_has_ended(
    backend: ReservationBackend,
) -> None:
    await backend.webhook_events.record(EVENT, now=NOW)

    first = await _claim(backend)
    during = await _claim(backend, LEASE)
    after = await _claim(backend, LEASE + timedelta(seconds=1))

    assert _ids(first.claimed) == [EVENT.event_id]
    assert during.claimed == ()
    assert [(e.event.event_id, e.attempts) for e in after.claimed] == [(EVENT.event_id, 2)]


async def test_an_event_out_of_attempts_is_failed_by_the_next_claim(
    backend: ReservationBackend,
) -> None:
    await backend.webhook_events.record(EVENT, now=NOW)
    step = LEASE + timedelta(seconds=1)

    claims = [await _claim(backend, n * step, max_attempts=2) for n in range(4)]

    assert [len(claim.claimed) for claim in claims] == [1, 1, 0, 0]
    [exhausted] = claims[2].exhausted
    assert (exhausted.attempts, exhausted.outcome) == (2, WebhookOutcome.FAILED)
    assert exhausted.processed_at == NOW + 2 * step
    assert claims[3].exhausted == ()
    assert await backend.webhook_event(EVENT.provider, EVENT.event_id) == exhausted


async def test_a_processed_event_is_never_claimed_and_keeps_its_first_outcome(
    backend: ReservationBackend,
) -> None:
    registry = backend.webhook_events
    await registry.record(EVENT, now=NOW)
    provider, event_id = EVENT.provider, EVENT.event_id

    first = await registry.mark_processed(provider, event_id, WebhookOutcome.IGNORED, now=NOW)
    again = await registry.mark_processed(provider, event_id, WebhookOutcome.APPLIED, now=NOW)

    assert (first, again) == (True, False)
    stored = await backend.webhook_event(provider, event_id)
    assert stored is not None
    assert (stored.outcome, stored.processed_at) == (WebhookOutcome.IGNORED, NOW)
    assert await _claim(backend, timedelta(hours=1)) == EventClaim()
    with pytest.raises(RecordNotFound):
        await registry.mark_processed(provider, "evt_404", WebhookOutcome.APPLIED, now=NOW)


async def test_claims_at_once_take_disjoint_events(backend: ReservationBackend) -> None:
    for n in range(30):
        await backend.webhook_events.record(_event(f"evt_{n:02d}"), now=NOW)

    claims = await asyncio.gather(*(_claim(backend) for _ in range(3)))

    claimed = [event_id for claim in claims for event_id in _ids(claim.claimed)]
    assert sorted(claimed) == [f"evt_{n:02d}" for n in range(30)]
    assert max(len(claim.claimed) for claim in claims) == 20


async def test_a_claim_needs_a_limit_a_lease_and_attempts(backend: ReservationBackend) -> None:
    registry = backend.webhook_events

    for limit, lease, attempts in ((0, LEASE, 10), (20, timedelta(0), 10), (20, LEASE, 0)):
        with pytest.raises(ValueError, match=r"positive|one event at least"):
            await registry.claim(now=NOW, limit=limit, lease=lease, max_attempts=attempts)


# --- Settlements -----------------------------------------------------------------------


@dataclass(frozen=True)
class Checkout:
    """A pending order of 2 x B07 at 12.50 EUR, and its attempt, open on session ``ref``."""

    order: OrderRecord
    product_id: UUID
    payment_id: UUID
    ref: str


async def _checkout(backend: ReservationBackend, *, ref: str = "mock_cs_1") -> Checkout:
    session = await backend.add_live_session()
    product = await backend.add_product(session.id, "B07", price=Money(1_250, "EUR"))
    comment_id = await backend.add_comment("comment-1")
    result = await backend.orders.reserve(
        ReservationRequest(
            comment_id=comment_id,
            session_id=session.id,
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
    opening = await backend.payments.open_attempt(
        result.order.id,
        provider=PaymentProviderName.MOCK,
        return_url="livecart://payment-return",
        now=NOW,
        grace=timedelta(seconds=600),
        max_attempts=3,
    )
    assert opening.payment is not None
    await backend.payments.record_checkout(
        opening.payment.id, provider_ref=ref, redirect_url=f"http://localhost:8080/{ref}"
    )
    return Checkout(result.order, product.id, opening.payment.id, ref)


def _paid(checkout: Checkout, event_id: str = "evt_paid", /, **fields: object) -> PaymentEvent:
    return _event(event_id, checkout_ref=checkout.ref, **fields)


async def _settle(
    backend: ReservationBackend,
    event: PaymentEvent,
    effect: PaymentEffect = PaymentEffect.PAID,
    *,
    at: timedelta = timedelta(minutes=1),
) -> PaymentSettlement | None:
    await backend.webhook_events.record(event, now=NOW)
    return await backend.webhook_events.settle(event, effect, now=NOW + at)


async def _stock(backend: ReservationBackend, checkout: Checkout) -> StockLevel:
    product = await backend.products.get(checkout.product_id)
    assert product is not None
    return product.stock


async def _attempt(backend: ReservationBackend, checkout: Checkout) -> PaymentRecord:
    attempt = await backend.payments.latest(checkout.order.id)
    assert attempt is not None
    return attempt


async def _outcome(backend: ReservationBackend, event: PaymentEvent) -> WebhookOutcome | None:
    stored = await backend.webhook_event(event.provider, event.event_id)
    assert stored is not None
    return stored.outcome


async def test_a_payment_sells_the_reserved_units_and_applies_the_event(
    backend: ReservationBackend,
) -> None:
    checkout = await _checkout(backend)
    event = _paid(checkout)

    settlement = await _settle(backend, event)

    assert settlement is not None
    assert settlement.kind is SettlementKind.APPLIED
    assert (settlement.order_changed, settlement.payment_changed) == (True, True)
    assert settlement.order is not None
    assert settlement.order.status is OrderStatus.PAID
    assert settlement.order.paid_at == NOW + timedelta(minutes=1)
    assert settlement.stock == StockLevel(10, reserved=0, sold=2)
    assert await _stock(backend, checkout) == StockLevel(10, reserved=0, sold=2)
    assert await backend.orders.get(checkout.order.id) == settlement.order
    attempt = await _attempt(backend, checkout)
    assert (attempt.status, attempt.provider_payment_ref) == (PaymentStatus.SUCCEEDED, "mock_pi_1")
    assert await _outcome(backend, event) == WebhookOutcome.APPLIED


async def test_an_event_applied_already_writes_nothing_and_its_replay_is_ignored(
    backend: ReservationBackend,
) -> None:
    checkout = await _checkout(backend)
    event = _paid(checkout)
    await _settle(backend, event)
    replay = _paid(
        checkout, "evt_async", event_type=CheckoutEventType.ASYNC_PAYMENT_SUCCEEDED.value
    )

    again = await backend.webhook_events.settle(event, PaymentEffect.PAID, now=NOW)
    replayed = await _settle(backend, replay, at=timedelta(minutes=2))

    assert again is None
    assert replayed is not None
    assert replayed.kind is SettlementKind.REPLAYED
    assert (replayed.order_changed, replayed.payment_changed, replayed.stock) == (
        False,
        False,
        None,
    )
    assert replayed.order is not None
    assert replayed.order.paid_at == NOW + timedelta(minutes=1)
    assert await _stock(backend, checkout) == StockLevel(10, reserved=0, sold=2)
    assert await _outcome(backend, replay) == WebhookOutcome.IGNORED


@pytest.mark.parametrize(
    "charged",
    [
        {"amount_minor": 2_400},
        {"currency": "USD"},
        {"amount_minor": None, "currency": None},
    ],
    ids=["amount", "currency", "none"],
)
async def test_a_payment_of_another_amount_fails_and_moves_nothing(
    backend: ReservationBackend, charged: dict[str, object]
) -> None:
    checkout = await _checkout(backend)
    event = _paid(checkout, **charged)

    settlement = await _settle(backend, event)

    assert settlement is not None
    assert settlement.kind is SettlementKind.AMOUNT_MISMATCH
    assert settlement.order is not None
    assert settlement.order == await backend.orders.get(checkout.order.id)
    assert settlement.order.status is OrderStatus.PENDING_PAYMENT
    assert (await _attempt(backend, checkout)).status is PaymentStatus.PENDING
    assert await _stock(backend, checkout) == StockLevel(10, reserved=2)
    assert await _outcome(backend, event) == WebhookOutcome.FAILED


@pytest.mark.parametrize(
    ("effect", "status"),
    [(PaymentEffect.FAILED, PaymentStatus.FAILED), (PaymentEffect.EXPIRED, PaymentStatus.EXPIRED)],
)
async def test_a_failed_or_expired_session_settles_its_attempt_only(
    backend: ReservationBackend, effect: PaymentEffect, status: PaymentStatus
) -> None:
    checkout = await _checkout(backend)
    event = _paid(checkout, payment_ref=None, amount_minor=None, currency=None)

    settlement = await _settle(backend, event, effect)

    assert settlement is not None
    assert settlement.kind is SettlementKind.APPLIED
    assert (settlement.order_changed, settlement.payment_changed) == (False, True)
    assert settlement.order is not None
    assert settlement.order.status is OrderStatus.PENDING_PAYMENT
    attempt = await _attempt(backend, checkout)
    assert (attempt.status, attempt.provider_payment_ref) == (status, None)
    assert await _stock(backend, checkout) == StockLevel(10, reserved=2)
    assert await _outcome(backend, event) == WebhookOutcome.APPLIED


async def test_an_event_out_of_order_is_stale(backend: ReservationBackend) -> None:
    checkout = await _checkout(backend)
    await _settle(backend, _paid(checkout))
    late = _paid(checkout, "evt_expired", event_type=CheckoutEventType.EXPIRED.value)

    settlement = await _settle(backend, late, PaymentEffect.EXPIRED)

    assert settlement is not None
    assert settlement.kind is SettlementKind.STALE
    assert (await _attempt(backend, checkout)).status is PaymentStatus.SUCCEEDED
    assert await _outcome(backend, late) == WebhookOutcome.IGNORED


async def test_a_payment_after_the_release_moves_no_stock(backend: ReservationBackend) -> None:
    checkout = await _checkout(backend)
    await backend.settle_payment(checkout.payment_id, PaymentStatus.EXPIRED)
    await backend.expire_order(checkout.order.id)
    event = _paid(checkout)

    settlement = await _settle(backend, event, at=TTL)

    assert settlement is not None
    assert settlement.kind is SettlementKind.APPLIED
    assert (settlement.order_changed, settlement.payment_changed, settlement.stock) == (
        True,
        False,
        None,
    )
    assert settlement.order is not None
    assert settlement.order.status is OrderStatus.PAID_AFTER_EXPIRY
    assert settlement.order.paid_at == NOW + TTL
    assert await _stock(backend, checkout) == StockLevel(10)
    # The attempt keeps its status, and records what the refund is made on.
    attempt = await _attempt(backend, checkout)
    assert (attempt.status, attempt.provider_payment_ref) == (PaymentStatus.EXPIRED, "mock_pi_1")
    assert await _outcome(backend, event) == WebhookOutcome.APPLIED


async def test_a_payment_after_the_release_keeps_a_payment_recorded_already(
    backend: ReservationBackend,
) -> None:
    checkout = await _checkout(backend)
    await backend.payments.set_status(
        checkout.payment_id, PaymentStatus.EXPIRED, provider_payment_ref="mock_pi_known"
    )
    await backend.expire_order(checkout.order.id)
    event = _paid(checkout, payment_ref=None)

    settlement = await _settle(backend, event, at=TTL)

    assert settlement is not None
    assert settlement.order is not None
    assert (settlement.kind, settlement.order.status) == (
        SettlementKind.APPLIED,
        OrderStatus.PAID_AFTER_EXPIRY,
    )
    attempt = await _attempt(backend, checkout)
    assert (attempt.status, attempt.provider_payment_ref) == (
        PaymentStatus.EXPIRED,
        "mock_pi_known",
    )


async def test_an_event_of_no_attempt_of_livecart_is_ignored(backend: ReservationBackend) -> None:
    checkout = await _checkout(backend)
    foreign = _event("evt_foreign", checkout_ref="cs_of_another_integration")
    without_session = _event("evt_no_session", checkout_ref=None)
    other_provider = _paid(checkout, "evt_stripe", provider=PaymentProviderName.STRIPE)

    settlements = [
        await _settle(backend, event) for event in (foreign, without_session, other_provider)
    ]

    for settlement in settlements:
        assert settlement is not None
        assert (settlement.kind, settlement.order) == (SettlementKind.UNKNOWN_SESSION, None)
    assert await _stock(backend, checkout) == StockLevel(10, reserved=2)
    assert await _outcome(backend, foreign) == WebhookOutcome.IGNORED
    with pytest.raises(RecordNotFound):
        await backend.webhook_events.settle(_event("evt_404"), PaymentEffect.PAID, now=NOW)


async def test_settlements_racing_each_other_apply_one_event_once(
    backend: ReservationBackend,
) -> None:
    checkout = await _checkout(backend)
    event = _paid(checkout)
    other = _paid(checkout, "evt_async", event_type=CheckoutEventType.ASYNC_PAYMENT_SUCCEEDED.value)
    for recorded in (event, other):
        await backend.webhook_events.record(recorded, now=NOW)

    settlements = await asyncio.gather(
        *(
            backend.webhook_events.settle(recorded, PaymentEffect.PAID, now=NOW)
            for recorded in (event, other, event, other, event)
        )
    )

    kinds = sorted(s.kind for s in settlements if s is not None)
    assert kinds == sorted([SettlementKind.APPLIED, SettlementKind.REPLAYED])
    assert settlements.count(None) == 3
    assert await _stock(backend, checkout) == StockLevel(10, reserved=0, sold=2)
