from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    AttemptOpening,
    AttemptOpeningKind,
    CarrierName,
    IntentSource,
    PaymentEvent,
    PaymentProviderName,
    ReservationRequest,
    ShipmentRef,
    WebhookOutcome,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import (
    PaymentStatus,
    ShipmentStatus,
    TransitionOutcome,
)
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FAKE_CLOCK_START, InMemoryDatabase

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
GRACE = timedelta(seconds=600)
UNKNOWN = UUID(int=404)
RETURN_URL = "livecart://payment-return"


async def _order(database: InMemoryDatabase, comment: str = "comment-1") -> UUID:
    """A pending order of 2 x B07 at 12.50 EUR, reserved until NOW + TTL."""
    session = next(iter(database.tables.sessions.values()), None) or database.add_live_session()
    if database.find_product(session.id, ProductCode("B07"), Variant()) is None:
        database.add_product(session.id, "B07", price=Money(1_250, "EUR"))
    envelope = queued_comment(external_comment_id=comment, author=f"buyer-of-{comment}")
    claim = await database.comments.claim(
        envelope, holder="worker-1", now=NOW, lease=timedelta(seconds=30)
    )
    result = await database.orders.reserve(
        ReservationRequest(
            comment_id=claim.comment.id,
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
    return result.order.id


async def _open(
    database: InMemoryDatabase,
    order_id: UUID,
    *,
    now: timedelta = timedelta(0),
    max_attempts: int = 3,
) -> AttemptOpening:
    return await database.payments.open_attempt(
        order_id,
        provider=PaymentProviderName.MOCK,
        return_url=RETURN_URL,
        now=NOW + now,
        grace=GRACE,
        max_attempts=max_attempts,
    )


# --- Payments --------------------------------------------------------------------------
# What every backend does is in tests/contract/test_payment_attempts.py; the
# settlement of an attempt and a paid order have no SQL adapter yet.


async def test_an_order_past_its_reservation_or_paid_is_not_payable() -> None:
    database = InMemoryDatabase()
    due, paid = await _order(database, "comment-1"), await _order(database, "comment-2")
    await database.orders.record_payment(paid, now=NOW)

    assert (await _open(database, due, now=TTL)).kind is AttemptOpeningKind.NOT_PAYABLE
    assert (await _open(database, paid)).kind is AttemptOpeningKind.NOT_PAYABLE
    assert database.tables.payments == {}


async def test_a_pending_attempt_settles_once() -> None:
    database = InMemoryDatabase()
    opening = await _open(database, await _order(database))
    assert opening.payment is not None
    payment_id = opening.payment.id

    succeeded = await database.payments.set_status(
        payment_id, PaymentStatus.SUCCEEDED, provider_payment_ref="fake_pi_1"
    )
    replayed = await database.payments.set_status(payment_id, PaymentStatus.SUCCEEDED)

    stored = database.tables.payments[payment_id]
    assert (succeeded, replayed) == (TransitionOutcome.APPLIED, TransitionOutcome.NOOP)
    assert (stored.status, stored.provider_payment_ref) == (PaymentStatus.SUCCEEDED, "fake_pi_1")
    with pytest.raises(InvalidTransition):
        await database.payments.set_status(payment_id, PaymentStatus.EXPIRED)
    with pytest.raises(RecordNotFound):
        await database.payments.set_status(UNKNOWN, PaymentStatus.FAILED)


async def test_a_settled_attempt_without_reference_keeps_the_one_it_had() -> None:
    database = InMemoryDatabase()
    opening = await _open(database, await _order(database))
    assert opening.payment is not None

    await database.payments.set_status(opening.payment.id, PaymentStatus.FAILED)

    assert database.tables.payments[opening.payment.id].provider_payment_ref is None


# --- Shipments -------------------------------------------------------------------------


def _ref(number: int) -> ShipmentRef:
    return ShipmentRef(CarrierName.MOCK, f"fake-shipment-{number}", f"FAKE{number:08d}", NOW)


async def test_an_order_has_one_shipment() -> None:
    database = InMemoryDatabase()
    order_id = await _order(database)

    first = await database.shipments.add(order_id, _ref(1))
    again = await database.shipments.add(order_id, _ref(2))

    assert again == first
    assert (first.status, first.ref, first.last_event_at) == (ShipmentStatus.CREATED, _ref(1), None)
    assert await database.shipments.get_by_order(order_id) == first
    assert await database.shipments.get_by_order(UNKNOWN) is None
    with pytest.raises(RecordNotFound):
        await database.shipments.add(UNKNOWN, _ref(3))


async def test_a_shipment_moves_along_its_machine() -> None:
    database = InMemoryDatabase()
    shipment = await database.shipments.add(await _order(database), _ref(1))
    event_at = NOW + timedelta(minutes=2)

    moved = await database.shipments.set_status(
        shipment.id, ShipmentStatus.IN_TRANSIT, event_at=event_at
    )
    again = await database.shipments.set_status(
        shipment.id, ShipmentStatus.IN_TRANSIT, event_at=event_at + timedelta(minutes=1)
    )

    stored = database.tables.shipments[shipment.id]
    assert (moved, again) == (TransitionOutcome.APPLIED, TransitionOutcome.NOOP)
    assert (stored.status, stored.last_event_at) == (ShipmentStatus.IN_TRANSIT, event_at)
    with pytest.raises(InvalidTransition):
        await database.shipments.set_status(
            shipment.id, ShipmentStatus.CANCELLED, event_at=event_at
        )
    with pytest.raises(RecordNotFound):
        await database.shipments.set_status(UNKNOWN, ShipmentStatus.IN_TRANSIT, event_at=NOW)


async def test_tracking_polls_shipments_still_in_flight_lowest_id_first() -> None:
    database = InMemoryDatabase()
    orders = [await _order(database, f"comment-{n}") for n in range(4)]
    shipments = [await database.shipments.add(order, _ref(n)) for n, order in enumerate(orders)]
    delivered, cancelled, moving, waiting = shipments
    for status in (
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.DELIVERED,
    ):
        await database.shipments.set_status(delivered.id, status, event_at=NOW)
    await database.shipments.set_status(cancelled.id, ShipmentStatus.CANCELLED, event_at=NOW)
    await database.shipments.set_status(moving.id, ShipmentStatus.IN_TRANSIT, event_at=NOW)

    in_flight = await database.shipments.list_in_flight(limit=10)

    assert [row.id for row in in_flight] == [moving.id, waiting.id]
    assert [row.id for row in await database.shipments.list_in_flight(limit=1)] == [moving.id]
    with pytest.raises(ValueError, match="limit"):
        await database.shipments.list_in_flight(limit=0)


# --- Webhook registry ------------------------------------------------------------------


def _event(event_id: str, event_type: str = "checkout.session.completed") -> PaymentEvent:
    return PaymentEvent(
        provider=PaymentProviderName.STRIPE,
        event_id=event_id,
        event_type=event_type,
        checkout_ref="cs_1",
        occurred_at=NOW,
    )


async def test_an_event_is_recorded_once_and_its_redeliveries_counted() -> None:
    registry = InMemoryDatabase().webhook_events
    first = _event("evt_1")

    assert await registry.record(first, now=NOW) is True
    assert await registry.record(_event("evt_1", "altered"), now=NOW) is False
    assert await registry.record(first, now=NOW) is False

    [registered] = await registry.pending(limit=10)
    assert registered.event == first
    assert registered.delivery_count == 3
    assert registered.received_at == NOW


async def test_pending_events_come_oldest_first_and_leave_once_processed() -> None:
    registry = InMemoryDatabase().webhook_events
    await registry.record(_event("evt_2"), now=NOW + timedelta(seconds=2))
    await registry.record(_event("evt_1"), now=NOW)
    await registry.record(_event("evt_3"), now=NOW + timedelta(seconds=2))

    assert [r.event.event_id for r in await registry.pending(limit=10)] == [
        "evt_1",
        "evt_2",
        "evt_3",
    ]
    assert [r.event.event_id for r in await registry.pending(limit=1)] == ["evt_1"]

    stripe = PaymentProviderName.STRIPE
    processed_at = NOW + timedelta(seconds=5)
    assert await registry.mark_processed(stripe, "evt_1", WebhookOutcome.APPLIED, now=processed_at)
    assert not await registry.mark_processed(stripe, "evt_1", WebhookOutcome.IGNORED, now=NOW)
    assert [r.event.event_id for r in await registry.pending(limit=10)] == ["evt_2", "evt_3"]


async def test_a_processed_event_keeps_its_first_outcome() -> None:
    database = InMemoryDatabase()
    registry = database.webhook_events
    stripe = PaymentProviderName.STRIPE
    await registry.record(_event("evt_1"), now=NOW)

    await registry.mark_processed(stripe, "evt_1", WebhookOutcome.IGNORED, now=NOW)
    await registry.mark_processed(stripe, "evt_1", WebhookOutcome.APPLIED, now=NOW)

    stored = database.tables.webhook_events[(stripe, "evt_1")]
    assert (stored.outcome, stored.processed_at) == (WebhookOutcome.IGNORED, NOW)


async def test_an_event_fails_for_good_at_the_attempt_limit() -> None:
    registry = InMemoryDatabase().webhook_events
    stripe = PaymentProviderName.STRIPE
    await registry.record(_event("evt_1"), now=NOW)

    first = await registry.record_failure(stripe, "evt_1", max_attempts=2, now=NOW)
    last = await registry.record_failure(stripe, "evt_1", max_attempts=2, now=NOW)
    after = await registry.record_failure(stripe, "evt_1", max_attempts=2, now=NOW)

    assert (first.attempts, first.outcome, first.processed_at) == (1, None, None)
    assert (last.attempts, last.outcome, last.processed_at) == (2, WebhookOutcome.FAILED, NOW)
    assert after == last
    assert await registry.pending(limit=10) == []


async def test_the_registry_knows_only_recorded_events() -> None:
    registry = InMemoryDatabase().webhook_events
    stripe = PaymentProviderName.STRIPE

    with pytest.raises(RecordNotFound):
        await registry.mark_processed(stripe, "evt_404", WebhookOutcome.APPLIED, now=NOW)
    with pytest.raises(RecordNotFound):
        await registry.record_failure(stripe, "evt_404", max_attempts=10, now=NOW)
    with pytest.raises(ValueError, match="limit"):
        await registry.pending(limit=0)
