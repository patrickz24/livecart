"""``ShipmentRepository`` on every backend: the in-memory fake, then PostgreSQL.

One shipment per order, the first one recorded, however many creations run
at once; a status moved one step at a time by compare-and-set; the paid
orders a crash left without a shipment, for the fallback; and the shipments
in flight, leased to one worker's tracking at a time.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    CarrierName,
    CheckoutEventType,
    CheckoutPaymentStatus,
    IntentSource,
    OrderRecord,
    PaymentEvent,
    PaymentProviderName,
    ReservationRequest,
    ShipmentRecord,
    ShipmentRef,
    ShippingAddress,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.money import Money
from livecart_core.domain.payments import PaymentEffect
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus, ShipmentStatus, TransitionOutcome
from livecart_core.testing.backends import ReservationBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
LEASE = timedelta(seconds=60)
UNKNOWN = UUID(int=404)
ADDRESS = ShippingAddress(
    full_name="Camille Martin",
    line1="12 rue des Lilas",
    postal_code="69003",
    city="Lyon",
    country="FR",
)


async def _session(backend: ReservationBackend) -> UUID:
    """A live session selling B07 at 12.50 EUR, with stock for every scenario."""
    session = await backend.add_live_session()
    await backend.add_product(session.id, "B07", price=Money(1_250, "EUR"), stock_total=100)
    return session.id


async def _order(backend: ReservationBackend, session_id: UUID, comment: str) -> OrderRecord:
    """A pending order of 2 x B07 for a buyer of its own, reserved until NOW + TTL."""
    comment_id = await backend.add_comment(comment, author=f"buyer-of-{comment}")
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


async def _paid(
    backend: ReservationBackend,
    session_id: UUID,
    comment: str,
    *,
    at: datetime = NOW,
    address: ShippingAddress | None = ADDRESS,
) -> OrderRecord:
    """An order paid at ``at``, by its attempt and the provider's event, as the registry pays."""
    order = await _order(backend, session_id, comment)
    if address is not None:
        await backend.orders.set_shipping_address(order.id, address)
    opening = await backend.payments.open_attempt(
        order.id,
        provider=PaymentProviderName.MOCK,
        return_url="livecart://payment-return",
        now=NOW,
        grace=timedelta(seconds=600),
        max_attempts=3,
    )
    assert opening.payment is not None
    provider_ref = f"cs_{order.reference.value}"
    await backend.payments.record_checkout(
        opening.payment.id, provider_ref=provider_ref, redirect_url=f"https://pay.invalid/{comment}"
    )
    event = PaymentEvent(
        provider=PaymentProviderName.MOCK,
        event_id=f"evt_{order.reference.value}",
        event_type=CheckoutEventType.COMPLETED.value,
        checkout_ref=provider_ref,
        payment_ref=f"pi_{order.reference.value}",
        payment_status=CheckoutPaymentStatus.PAID,
        amount_minor=order.total.amount_minor,
        currency=order.total.currency,
        occurred_at=at,
    )
    await backend.webhook_events.record(event, now=at)
    settled = await backend.webhook_events.settle(event, PaymentEffect.PAID, now=at)
    assert settled is not None
    assert settled.order is not None
    assert settled.order.status is OrderStatus.PAID
    return settled.order


def _ref(number: int, *, at: datetime = NOW) -> ShipmentRef:
    return ShipmentRef(CarrierName.MOCK, f"mock_shp_{number:032x}", f"MOCK-{number:016d}", at)


async def _shipment(
    backend: ReservationBackend, session_id: UUID, comment: str, number: int
) -> ShipmentRecord:
    order = await _paid(backend, session_id, comment)
    added = await backend.shipments.add(order.id, _ref(number))
    return added.shipment


# --- Creation --------------------------------------------------------------------------


async def test_an_order_records_one_shipment_the_first_one(backend: ReservationBackend) -> None:
    session_id = await _session(backend)
    order = await _paid(backend, session_id, "comment-1")

    first = await backend.shipments.add(order.id, _ref(1))
    again = await backend.shipments.add(order.id, _ref(2, at=NOW + timedelta(seconds=30)))

    assert first.outcome is TransitionOutcome.APPLIED
    shipment = first.shipment
    assert (shipment.order_id, shipment.ref, shipment.status) == (
        order.id,
        _ref(1),
        ShipmentStatus.CREATED,
    )
    # A shipment's creation is its first event.
    assert shipment.last_event_at == NOW
    assert again == replace(first, outcome=TransitionOutcome.NOOP)
    assert await backend.shipments.get_by_order(order.id) == shipment
    assert await backend.shipments.get_by_order(UNKNOWN) is None
    with pytest.raises(RecordNotFound):
        await backend.shipments.add(UNKNOWN, _ref(3))


async def test_creations_at_once_record_one_shipment(backend: ReservationBackend) -> None:
    session_id = await _session(backend)
    order = await _paid(backend, session_id, "comment-1")

    added = await asyncio.gather(
        *(backend.shipments.add(order.id, _ref(number)) for number in range(1, 11))
    )

    assert [change.outcome for change in added].count(TransitionOutcome.APPLIED) == 1
    assert len({change.shipment for change in added}) == 1
    assert await backend.shipments.get_by_order(order.id) == added[0].shipment


# --- Status ----------------------------------------------------------------------------


async def test_a_shipment_moves_one_step_at_a_time_by_compare_and_set(
    backend: ReservationBackend,
) -> None:
    session_id = await _session(backend)
    shipment = await _shipment(backend, session_id, "comment-1", 1)
    picked_up = NOW + timedelta(minutes=2)

    moved = await backend.shipments.set_status(
        shipment.id, ShipmentStatus.IN_TRANSIT, event_at=picked_up
    )
    again = await backend.shipments.set_status(
        shipment.id, ShipmentStatus.IN_TRANSIT, event_at=picked_up + timedelta(minutes=1)
    )

    assert moved.outcome is TransitionOutcome.APPLIED
    assert moved.shipment == replace(
        shipment, status=ShipmentStatus.IN_TRANSIT, last_event_at=picked_up
    )
    assert again == replace(moved, outcome=TransitionOutcome.NOOP)
    with pytest.raises(InvalidTransition):
        await backend.shipments.set_status(shipment.id, ShipmentStatus.CANCELLED, event_at=NOW)
    with pytest.raises(InvalidTransition):
        await backend.shipments.set_status(shipment.id, ShipmentStatus.DELIVERED, event_at=NOW)
    with pytest.raises(RecordNotFound):
        await backend.shipments.set_status(UNKNOWN, ShipmentStatus.IN_TRANSIT, event_at=NOW)
    assert await backend.shipments.get_by_order(shipment.order_id) == moved.shipment


async def test_of_two_moves_at_once_one_applies(backend: ReservationBackend) -> None:
    session_id = await _session(backend)
    shipment = await _shipment(backend, session_id, "comment-1", 1)

    moves = await asyncio.gather(
        *(
            backend.shipments.set_status(
                shipment.id, ShipmentStatus.IN_TRANSIT, event_at=NOW + timedelta(seconds=second)
            )
            for second in range(1, 6)
        )
    )

    assert [move.outcome for move in moves].count(TransitionOutcome.APPLIED) == 1
    assert len({move.shipment for move in moves}) == 1


# --- The fallback's claim --------------------------------------------------------------


async def test_the_fallback_finds_the_paid_orders_left_without_a_shipment(
    backend: ReservationBackend,
) -> None:
    session_id = await _session(backend)
    late = NOW + timedelta(minutes=5)
    first = await _paid(backend, session_id, "comment-1")
    shipped = await _paid(backend, session_id, "comment-2")
    await backend.shipments.add(shipped.id, _ref(2))
    second = await _paid(backend, session_id, "comment-3")
    await _paid(backend, session_id, "comment-4", at=late)
    await _paid(backend, session_id, "comment-5", address=None)
    await _order(backend, session_id, "comment-6")
    expired = await _order(backend, session_id, "comment-7")
    await backend.expire_order(expired.id)

    found = await backend.shipments.unshipped_orders(paid_before=late, limit=10)
    limited = await backend.shipments.unshipped_orders(paid_before=late, limit=1)

    assert [order.id for order in found] == [first.id, second.id]
    assert found[0] == first
    assert [order.id for order in limited] == [first.id]
    later = await backend.shipments.unshipped_orders(paid_before=late + timedelta(1), limit=10)
    assert len(later) == 3


# --- The tracking's lease --------------------------------------------------------------


async def test_tracking_leases_the_shipments_in_flight_lowest_id_first(
    backend: ReservationBackend,
) -> None:
    session_id = await _session(backend)
    shipments = [
        await _shipment(backend, session_id, f"comment-{number}", number) for number in range(5)
    ]
    delivered, cancelled, moving, waiting, out = shipments
    for status in (
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.DELIVERED,
    ):
        await backend.shipments.set_status(delivered.id, status, event_at=NOW)
    await backend.shipments.set_status(cancelled.id, ShipmentStatus.CANCELLED, event_at=NOW)
    await backend.shipments.set_status(moving.id, ShipmentStatus.IN_TRANSIT, event_at=NOW)
    for status in (ShipmentStatus.IN_TRANSIT, ShipmentStatus.OUT_FOR_DELIVERY):
        await backend.shipments.set_status(out.id, status, event_at=NOW)

    leased = await backend.shipments.lease_for_tracking(now=NOW, limit=10, lease=LEASE)

    assert [item.shipment.id for item in leased] == [moving.id, waiting.id, out.id]
    order = await backend.orders.get(waiting.order_id)
    assert order is not None
    assert leased[1].shipment == waiting
    assert (leased[1].session_id, leased[1].reference) == (session_id, order.reference)
    assert {item.lease_until for item in leased} == {NOW + LEASE}


async def test_a_leased_shipment_is_no_other_lease_until_its_end(
    backend: ReservationBackend,
) -> None:
    session_id = await _session(backend)
    first = await _shipment(backend, session_id, "comment-1", 1)
    second = await _shipment(backend, session_id, "comment-2", 2)

    taken = await backend.shipments.lease_for_tracking(now=NOW, limit=1, lease=LEASE)
    rest = await backend.shipments.lease_for_tracking(now=NOW, limit=10, lease=LEASE)
    during = await backend.shipments.lease_for_tracking(now=NOW + LEASE, limit=10, lease=LEASE)
    after = await backend.shipments.lease_for_tracking(
        now=NOW + LEASE + timedelta(seconds=1), limit=10, lease=LEASE
    )

    assert [item.shipment.id for item in taken] == [first.id]
    assert [item.shipment.id for item in rest] == [second.id]
    assert during == ()
    assert [item.shipment.id for item in after] == [first.id, second.id]


async def test_leases_at_once_take_disjoint_shipments(backend: ReservationBackend) -> None:
    session_id = await _session(backend)
    shipments = {
        (await _shipment(backend, session_id, f"comment-{number}", number)).id
        for number in range(6)
    }

    leases = await asyncio.gather(
        *(backend.shipments.lease_for_tracking(now=NOW, limit=2, lease=LEASE) for _ in range(4))
    )

    taken = [item.shipment.id for lease in leases for item in lease]
    assert sorted(taken) == sorted(shipments)
