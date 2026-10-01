"""``SyncShipmentTracking`` over the in-memory fakes, on a frozen clock moved step by step.

The lease and the compare-and-set on PostgreSQL too are in
``tests/contract/test_shipment_repository.py``; the mock carrier's own
route, computed from the clock, in ``tests/unit/test_carriers.py``.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from livecart_core.application.create_shipment import CreateShipment
from livecart_core.application.dto import (
    LeasedShipment,
    SessionEvent,
    SessionEventType,
    ShipmentRecord,
    ShipmentRef,
    TrackingUpdate,
)
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.application.sync_shipment_tracking import (
    TRACKING_LEASE_MARGIN,
    SyncShipmentTracking,
    TrackingOutcome,
)
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.shipments import SHIPMENT_ROUTE
from livecart_core.domain.state_machines import ShipmentStatus
from livecart_core.testing.builders import paid_order
from livecart_core.testing.fakes import (
    FakeCarrierProvider,
    FakeClock,
    FakeEventPublisher,
    InMemoryDatabase,
    InMemoryShipmentRepository,
)

INTERVAL = timedelta(seconds=60)
STEP = timedelta(seconds=120)


class YieldingCarrier(FakeCarrierProvider):
    """The fake carrier, letting the other tasks run before it answers, as a network call does."""

    async def get_tracking(self, ref: ShipmentRef) -> TrackingUpdate:
        await asyncio.sleep(0)
        return await super().get_tracking(ref)


class YieldingEvents(FakeEventPublisher):
    """The fake publisher, letting the other tasks run before it appends, as Redis does."""

    async def publish(self, event: SessionEvent) -> None:
        await asyncio.sleep(0)
        await super().publish(event)


class World:
    """Paid orders, their carrier, the session streams, and one worker's tracking."""

    def __init__(
        self,
        shipments: type[InMemoryShipmentRepository] = InMemoryShipmentRepository,
        *,
        carrier: type[FakeCarrierProvider] = FakeCarrierProvider,
        events: type[FakeEventPublisher] = FakeEventPublisher,
    ) -> None:
        self.clock = FakeClock()
        self.database = InMemoryDatabase()
        self.shipments = shipments(self.database)
        self.carrier = carrier(self.clock)
        self.events = events()
        self.create = CreateShipment(self.shipments, self.carrier, self.events, self.clock)

    def tracking(self, *, lease: timedelta = INTERVAL, batch: int = 50) -> SyncShipmentTracking:
        return SyncShipmentTracking(
            self.shipments,
            self.carrier,
            self.events,
            self.clock,
            lease=lease,
            budget=INTERVAL,
            batch=batch,
        )

    async def shipment(self, comment: str = "comment-1") -> ShipmentRecord:
        created = await self.create(await paid_order(self.database, comment=comment))
        assert created.shipment is not None
        return created.shipment

    def moves(self) -> list[tuple[str, object]]:
        """The status and the latest event's time of each ``shipment.updated`` after a creation."""
        return [
            (str(event.data["status"]), event.data["last_event_at"])
            for event in self.events.of_type(SessionEventType.SHIPMENT_UPDATED)
            if event.data["status"] != "created"
        ]

    def stored(self, shipment: ShipmentRecord) -> ShipmentRecord:
        return self.database.tables.shipments[shipment.id]


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def test_every_transition_is_persisted_and_told_once_on_a_frozen_clock() -> None:
    world = World()
    shipment = await world.shipment()
    tracking = world.tracking()
    created_at = shipment.ref.created_at

    first = await tracking()
    for status in SHIPMENT_ROUTE[1:]:
        world.clock.advance(STEP)
        world.carrier.advance(shipment.ref, status)
        tick = await tracking()
        [tracked] = tick.tracked
        assert tracked.outcome is TrackingOutcome.MOVED
        assert [move.status for move in tracked.moves] == [status]
        assert world.stored(shipment).status is status
        assert world.stored(shipment).last_event_at == world.clock.now()
    world.clock.advance(STEP)
    after = await tracking()

    assert [tracked.outcome for tracked in first.tracked] == [TrackingOutcome.UNCHANGED]
    assert world.moves() == [
        ("in_transit", _iso(created_at + STEP)),
        ("out_for_delivery", _iso(created_at + 2 * STEP)),
        ("delivered", _iso(created_at + 3 * STEP)),
    ]
    # Delivered for good: no longer in flight, no longer asked about.
    assert after.tracked == ()
    [event] = world.events.of_type(SessionEventType.SHIPMENT_UPDATED)[-1:]
    assert (event.session_id, event.correlation_id) == (
        world.database.tables.orders[shipment.order_id].session_id,
        None,
    )


async def test_a_shipment_found_further_on_walks_every_step_between() -> None:
    world = World()
    shipment = await world.shipment()
    world.clock.advance(3 * STEP)
    world.carrier.advance(shipment.ref, ShipmentStatus.IN_TRANSIT)
    world.carrier.advance(shipment.ref, ShipmentStatus.OUT_FOR_DELIVERY)

    [tracked] = (await world.tracking()()).tracked

    assert tracked.outcome is TrackingOutcome.MOVED
    assert [move.status for move in tracked.moves] == [
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
    ]
    # Each step stamped with the time of the carrier's latest event.
    assert world.moves() == [
        ("in_transit", _iso(world.clock.now())),
        ("out_for_delivery", _iso(world.clock.now())),
    ]
    assert world.stored(shipment).status is ShipmentStatus.OUT_FOR_DELIVERY


async def test_a_leased_shipment_is_no_other_workers_until_its_lease_ends() -> None:
    world = World()
    await world.shipment()
    mine, theirs = world.tracking(), world.tracking()

    taken = await mine()
    meanwhile = await theirs()
    world.clock.advance(INTERVAL + timedelta(seconds=1))
    later = await theirs()

    assert len(taken.tracked) == 1
    assert meanwhile.tracked == ()
    assert len(later.tracked) == 1


class ExpiredLeases(InMemoryShipmentRepository):
    """Every lease handed out at once, as leases that all ended would: the race of the moves."""

    async def lease_for_tracking(
        self, *, now: datetime, limit: int, lease: timedelta
    ) -> tuple[LeasedShipment, ...]:
        self._tables.shipment_tracking_leases.clear()
        return await super().lease_for_tracking(now=now, limit=limit, lease=lease)


async def test_workers_racing_on_one_shipment_tell_each_transition_once() -> None:
    world = World(ExpiredLeases, carrier=YieldingCarrier, events=YieldingEvents)
    shipment = await world.shipment()
    world.clock.advance(3 * STEP)
    for status in (
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.OUT_FOR_DELIVERY,
        ShipmentStatus.DELIVERED,
    ):
        world.carrier.advance(shipment.ref, status)

    ticks = await asyncio.gather(*(world.tracking()() for _ in range(5)))

    moved = [move.status for tick in ticks for tracked in tick.tracked for move in tracked.moves]
    assert sorted(moved) == sorted(
        [ShipmentStatus.IN_TRANSIT, ShipmentStatus.OUT_FOR_DELIVERY, ShipmentStatus.DELIVERED]
    )
    assert sorted(status for status, _ in world.moves()) == [
        "delivered",
        "in_transit",
        "out_for_delivery",
    ]
    outcomes = [tracked.outcome for tick in ticks for tracked in tick.tracked]
    # Every worker followed the shipment: the compare-and-set, not a lease, kept them apart.
    assert len(outcomes) == 5
    assert set(outcomes) <= {TrackingOutcome.MOVED, TrackingOutcome.STALE}
    assert world.stored(shipment).status is ShipmentStatus.DELIVERED


async def test_a_carrier_down_is_asked_again_after_the_lease() -> None:
    world = World()
    shipment = await world.shipment()
    world.carrier.advance(shipment.ref, ShipmentStatus.IN_TRANSIT)
    world.carrier.fail_next()
    tracking = world.tracking()

    [down] = (await tracking()).tracked
    world.clock.advance(INTERVAL + timedelta(seconds=1))
    [back] = (await tracking()).tracked

    assert (down.outcome, down.moves) == (TrackingOutcome.RETRY, ())
    assert isinstance(down.error, ServiceUnavailable)
    assert back.outcome is TrackingOutcome.MOVED


class BackwardsCarrier(FakeCarrierProvider):
    """A carrier whose every answer is a parcel back at its label: a status the machine refuses."""

    async def get_tracking(self, ref: ShipmentRef) -> TrackingUpdate:
        return TrackingUpdate(ShipmentStatus.CREATED, ref.created_at)


async def test_a_status_the_machine_cannot_reach_is_a_failure_and_nothing_moves() -> None:
    world = World()
    shipment = await world.shipment()
    await world.shipments.set_status(
        shipment.id, ShipmentStatus.IN_TRANSIT, event_at=world.clock.now()
    )
    tracking = SyncShipmentTracking(
        world.shipments,
        BackwardsCarrier(world.clock),
        world.events,
        world.clock,
        lease=INTERVAL,
        budget=INTERVAL,
    )

    [tracked] = (await tracking()).tracked

    assert tracked.outcome is TrackingOutcome.FAILED
    assert isinstance(tracked.error, InvalidTransition)
    assert world.stored(shipment).status is ShipmentStatus.IN_TRANSIT
    assert world.moves() == []


async def test_a_ref_the_carrier_does_not_know_is_a_failure() -> None:
    world = World()
    await world.shipment()
    tracking = SyncShipmentTracking(
        world.shipments,
        FakeCarrierProvider(world.clock),
        world.events,
        world.clock,
        lease=INTERVAL,
        budget=INTERVAL,
    )

    [tracked] = (await tracking()).tracked

    assert tracked.outcome is TrackingOutcome.FAILED


class SlowCarrier(FakeCarrierProvider):
    async def get_tracking(self, ref: ShipmentRef) -> TrackingUpdate:
        await asyncio.sleep(60)
        return await super().get_tracking(ref)


async def test_a_claim_stops_before_its_lease_ends() -> None:
    world = World()
    await world.shipment()
    tracking = SyncShipmentTracking(
        world.shipments,
        SlowCarrier(world.clock),
        world.events,
        world.clock,
        lease=TRACKING_LEASE_MARGIN + timedelta(milliseconds=20),
        budget=INTERVAL,
    )

    [tracked] = (await tracking()).tracked

    assert tracked.outcome is TrackingOutcome.RETRY
    assert isinstance(tracked.error, TimeoutError)


async def test_a_failed_publication_is_counted_and_the_move_stays() -> None:
    world = World()
    shipment = await world.shipment()
    world.carrier.advance(shipment.ref, ShipmentStatus.IN_TRANSIT)
    world.events.fail_next()

    [tracked] = (await world.tracking()()).tracked

    assert (tracked.outcome, tracked.unpublished_events) == (TrackingOutcome.MOVED, 1)
    assert world.stored(shipment).status is ShipmentStatus.IN_TRANSIT


async def test_a_tick_leases_again_while_its_claims_come_back_full() -> None:
    world = World()
    for number in range(3):
        await world.shipment(f"comment-{number}")

    tick = await world.tracking(batch=2)()

    assert (tick.claims, len(tick.tracked)) == (2, 3)
    assert tick.count(TrackingOutcome.UNCHANGED) == 3


@pytest.mark.parametrize(
    ("lease", "budget", "batch"),
    [
        (TRACKING_LEASE_MARGIN, INTERVAL, 50),
        (INTERVAL, timedelta(0), 50),
        (INTERVAL, INTERVAL, 0),
    ],
)
def test_a_tick_needs_a_lease_past_its_margin_a_budget_and_a_batch(
    lease: timedelta, budget: timedelta, batch: int
) -> None:
    world = World()

    with pytest.raises(ValueError, match=r"at least|lease longer"):
        SyncShipmentTracking(
            world.shipments,
            world.carrier,
            world.events,
            world.clock,
            lease=lease,
            budget=budget,
            batch=batch,
        )
