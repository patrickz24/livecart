"""``CreateShipment`` and its fallback ``ShipUnshippedOrders``, over the in-memory fakes.

What the repository promises on PostgreSQL too, the creations at once
among it, is in ``tests/contract/test_shipment_repository.py``.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest

from livecart_core.application.create_shipment import (
    SHIPMENT_FALLBACK_GRACE,
    CreateShipment,
    ShipmentCreationOutcome,
    ShipUnshippedOrders,
)
from livecart_core.application.dto import (
    SessionEventType,
    ShipmentRef,
    ShipmentRequest,
    ShipmentTransition,
    TrackingUpdate,
)
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.domain.state_machines import OrderStatus, ShipmentStatus
from livecart_core.testing.builders import TEST_ADDRESS, paid_order
from livecart_core.testing.fakes import (
    FakeCarrierProvider,
    FakeClock,
    FakeEventPublisher,
    InMemoryDatabase,
    InMemoryShipmentRepository,
)


class SlowCarrier(FakeCarrierProvider):
    """A carrier that answers after a minute."""

    async def create_shipment(self, request: ShipmentRequest) -> ShipmentRef:
        await asyncio.sleep(60)
        return await super().create_shipment(request)


class DownShipments(InMemoryShipmentRepository):
    """The repository, whose every record fails as a database that is down."""

    async def add(self, order_id: UUID, ref: ShipmentRef) -> ShipmentTransition:
        raise ServiceUnavailable("database unavailable")


class Harness:
    """A paid order's world: the database, the clock, the carrier and the session streams."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.database = InMemoryDatabase()
        self.carrier = FakeCarrierProvider(self.clock)
        self.events = FakeEventPublisher()
        self.create = CreateShipment(self.database.shipments, self.carrier, self.events, self.clock)
        self.fallback = ShipUnshippedOrders(self.database.shipments, self.create, self.clock)


# --- CreateShipment --------------------------------------------------------------------


async def test_a_paid_order_gets_its_shipment_created_and_published_once() -> None:
    world = Harness()
    order = await paid_order(world.database)

    created = await world.create(order)
    again = await world.create(order)

    assert created.outcome is ShipmentCreationOutcome.CREATED
    shipment = created.shipment
    assert shipment is not None
    assert (shipment.order_id, shipment.status) == (order.id, ShipmentStatus.CREATED)
    assert world.carrier.requests[0] == ShipmentRequest(order.id, order.reference, TEST_ADDRESS)
    assert await world.database.shipments.get_by_order(order.id) == shipment
    # The second creation asked the carrier again, which named the same shipment.
    assert (again.outcome, again.shipment) == (ShipmentCreationOutcome.EXISTING, shipment)
    assert len(world.carrier.requests) == 2
    [event] = world.events.published
    assert (event.type, event.session_id, event.correlation_id) == (
        SessionEventType.SHIPMENT_UPDATED,
        order.session_id,
        None,
    )
    assert event.data == {
        "shipment_id": str(shipment.id),
        "order_id": str(order.id),
        "status": "created",
        "carrier": "mock",
        "tracking_number": shipment.ref.tracking_number,
        "last_event_at": shipment.last_event_at.isoformat().replace("+00:00", "Z"),
    }


async def test_creations_at_once_record_and_publish_one_shipment() -> None:
    world = Harness()
    order = await paid_order(world.database)

    created = await asyncio.gather(*(world.create(order) for _ in range(10)))

    outcomes = [item.outcome for item in created]
    assert outcomes.count(ShipmentCreationOutcome.CREATED) == 1
    assert outcomes.count(ShipmentCreationOutcome.EXISTING) == 9
    assert len(world.database.tables.shipments) == 1
    assert len(world.events.of_type(SessionEventType.SHIPMENT_UPDATED)) == 1


async def test_a_paid_order_without_an_address_needs_a_person() -> None:
    world = Harness()
    order = await paid_order(world.database, address=None)

    created = await world.create(order)

    assert (created.outcome, created.shipment) == (ShipmentCreationOutcome.NO_ADDRESS, None)
    assert world.carrier.requests == []
    assert world.events.published == []


async def test_only_a_paid_order_ships() -> None:
    world = Harness()
    order = replace(await paid_order(world.database), status=OrderStatus.PENDING_PAYMENT)

    with pytest.raises(ValueError, match="only a paid order ships"):
        await world.create(order)


async def test_a_carrier_down_leaves_the_order_to_the_fallback() -> None:
    world = Harness()
    order = await paid_order(world.database)
    world.carrier.fail_next()

    created = await world.create(order)

    assert created.outcome is ShipmentCreationOutcome.RETRY
    assert isinstance(created.error, ServiceUnavailable)
    assert world.database.tables.shipments == {}
    assert world.events.published == []


async def test_a_database_down_after_the_carrier_leaves_the_order_to_the_fallback() -> None:
    world = Harness()
    order = await paid_order(world.database)
    create = CreateShipment(DownShipments(world.database), world.carrier, world.events, world.clock)

    created = await create(order)

    assert created.outcome is ShipmentCreationOutcome.RETRY
    # The carrier created it: the next creation names the same shipment.
    assert len(world.carrier.requests) == 1
    assert world.database.tables.shipments == {}


async def test_a_carrier_too_slow_is_cut_at_its_timeout() -> None:
    world = Harness()
    order = await paid_order(world.database)
    create = CreateShipment(
        world.database.shipments,
        SlowCarrier(world.clock),
        world.events,
        world.clock,
        call_timeout=timedelta(milliseconds=10),
    )

    created = await create(order)

    assert created.outcome is ShipmentCreationOutcome.RETRY
    assert isinstance(created.error, TimeoutError)


async def test_any_other_failure_is_reported_and_left_to_the_fallback() -> None:
    world = Harness()
    order = await paid_order(world.database)
    # An order the database no longer has: the record fails, and no retry would help.
    world.database.tables.orders.pop(order.id)

    created = await world.create(order)

    assert created.outcome is ShipmentCreationOutcome.FAILED
    assert created.error is not None


async def test_a_failed_publication_is_counted_and_the_shipment_stays() -> None:
    world = Harness()
    order = await paid_order(world.database)
    world.events.fail_next()

    created = await world.create(order)

    assert (created.outcome, created.unpublished_events) == (ShipmentCreationOutcome.CREATED, 1)
    assert await world.database.shipments.get_by_order(order.id) is not None


async def test_the_carrier_tracks_what_was_recorded() -> None:
    world = Harness()
    order = await paid_order(world.database)

    created = await world.create(order)

    assert created.shipment is not None
    assert await world.carrier.get_tracking(created.shipment.ref) == TrackingUpdate(
        ShipmentStatus.CREATED, created.shipment.last_event_at
    )


def test_a_carriers_call_needs_time() -> None:
    world = Harness()

    with pytest.raises(ValueError, match="needs time"):
        CreateShipment(
            world.database.shipments,
            world.carrier,
            world.events,
            world.clock,
            call_timeout=timedelta(0),
        )


# --- The fallback ----------------------------------------------------------------------


async def test_the_fallback_ships_the_paid_orders_a_crash_left_behind() -> None:
    world = Harness()
    shipped = await paid_order(world.database, comment="comment-1")
    await world.create(shipped)
    crashed = await paid_order(world.database, comment="comment-2")
    await paid_order(world.database, comment="comment-3", address=None)
    world.clock.advance(SHIPMENT_FALLBACK_GRACE + timedelta(seconds=1))

    created = await world.fallback()
    again = await world.fallback()

    assert [(item.order.id, item.outcome) for item in created] == [
        (crashed.id, ShipmentCreationOutcome.CREATED)
    ]
    assert again == ()
    assert len(world.events.of_type(SessionEventType.SHIPMENT_UPDATED)) == 2


async def test_the_fallback_leaves_a_fresh_payment_to_its_own_path() -> None:
    world = Harness()
    await paid_order(world.database)

    world.clock.advance(SHIPMENT_FALLBACK_GRACE)
    fresh = await world.fallback()
    world.clock.advance(timedelta(seconds=1))
    late = await world.fallback()

    assert fresh == ()
    assert [item.outcome for item in late] == [ShipmentCreationOutcome.CREATED]


async def test_the_fallback_takes_one_batch_a_tick() -> None:
    world = Harness()
    for number in range(3):
        await paid_order(world.database, comment=f"comment-{number}")
    world.clock.advance(SHIPMENT_FALLBACK_GRACE + timedelta(seconds=1))
    fallback = ShipUnshippedOrders(world.database.shipments, world.create, world.clock, batch=2)

    first = await fallback()
    second = await fallback()

    assert (len(first), len(second)) == (2, 1)


async def test_the_fallback_asks_a_carrier_down_again_next_tick() -> None:
    world = Harness()
    await paid_order(world.database)
    world.clock.advance(SHIPMENT_FALLBACK_GRACE + timedelta(seconds=1))
    world.carrier.fail_next()

    down = await world.fallback()
    back = await world.fallback()

    assert [item.outcome for item in down] == [ShipmentCreationOutcome.RETRY]
    assert [item.outcome for item in back] == [ShipmentCreationOutcome.CREATED]


def test_the_fallback_takes_one_order_at_least() -> None:
    world = Harness()

    with pytest.raises(ValueError, match="one order at least"):
        ShipUnshippedOrders(world.database.shipments, world.create, world.clock, batch=0)
