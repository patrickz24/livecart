"""The shipments of the paid orders from the worker's loops: logs, metrics, the fallback's ticks."""

import asyncio
from datetime import datetime, timedelta

import pytest
import structlog

from livecart_core.application.create_shipment import (
    SHIPMENT_FALLBACK_GRACE,
    CreateShipment,
    ShipUnshippedOrders,
)
from livecart_core.application.dto import (
    LeasedShipment,
    OrderRecord,
    SessionEventType,
    ShipmentRecord,
)
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.application.sync_shipment_tracking import SyncShipmentTracking
from livecart_core.domain.state_machines import ShipmentStatus
from livecart_core.testing.builders import paid_order
from livecart_core.testing.fakes import (
    FakeCarrierProvider,
    FakeClock,
    FakeEventPublisher,
    InMemoryDatabase,
    InMemoryShipmentRepository,
)
from livecart_core.testing.polling import until
from livecart_worker.metrics import WorkerMetrics
from livecart_worker.shipments import (
    run_shipments,
    ship_paid,
    ship_unshipped_once,
    track_once,
)


class World:
    def __init__(self, shipments: type[InMemoryShipmentRepository] = InMemoryShipmentRepository):
        self.clock = FakeClock()
        self.database = InMemoryDatabase()
        self.shipments = shipments(self.database)
        self.carrier = FakeCarrierProvider(self.clock)
        self.events = FakeEventPublisher()
        self.metrics = WorkerMetrics()
        self.ship = CreateShipment(self.shipments, self.carrier, self.events, self.clock)
        self.fallback = ShipUnshippedOrders(self.shipments, self.ship, self.clock)

    def sample(self, name: str, **labels: str) -> float | None:
        return self.metrics.registry.get_sample_value(name, labels)

    def creations(self, outcome: str, via: str) -> float | None:
        return self.sample("livecart_shipment_creations_total", outcome=outcome, via=via)


async def test_the_orders_a_tick_paid_are_shipped_logged_and_counted() -> None:
    world = World()
    orders = [await paid_order(world.database, comment=f"comment-{n}") for n in range(2)]

    with structlog.testing.capture_logs() as logs:
        created = await ship_paid(world.ship, orders, world.metrics)

    assert [log["event"] for log in logs] == ["shipment_created", "shipment_created"]
    shipment = created[0].shipment
    assert shipment is not None
    assert logs[0] == {
        "event": "shipment_created",
        "log_level": "info",
        "order_id": str(orders[0].id),
        "reference": orders[0].reference.value,
        "via": "payment",
        "outcome": "created",
        "shipment_id": str(shipment.id),
        "carrier": "mock",
    }
    # The tracking number follows a parcel to the buyer's door: never a log field.
    assert shipment.ref.tracking_number not in str(logs)
    assert world.creations("created", "payment") == 2
    assert world.creations("created", "fallback") == 0
    assert len(world.events.of_type(SessionEventType.SHIPMENT_UPDATED)) == 2


async def test_what_needs_a_person_is_an_error_and_an_outage_a_warning() -> None:
    world = World()
    no_address = await paid_order(world.database, comment="comment-1", address=None)
    down = await paid_order(world.database, comment="comment-2")
    world.carrier.fail_next()

    with structlog.testing.capture_logs() as logs:
        await ship_paid(world.ship, [no_address, down], world.metrics)

    assert [(log["event"], log["log_level"]) for log in logs] == [
        ("shipment_without_address", "error"),
        ("shipment_creation_deferred", "warning"),
    ]
    assert logs[1]["error"] == "ServiceUnavailable"
    assert world.creations("no_address", "payment") == 1
    assert world.creations("retry", "payment") == 1


class LostOrders(InMemoryShipmentRepository):
    """A repository that no longer finds the orders it is asked to ship."""

    async def unshipped_orders(
        self, *, paid_before: datetime, limit: int
    ) -> tuple[OrderRecord, ...]:
        orders = await super().unshipped_orders(paid_before=paid_before, limit=limit)
        for order in orders:
            self._db.tables.orders.pop(order.id)
        return orders


async def test_a_failed_creation_is_an_error_with_its_cause() -> None:
    world = World(LostOrders)
    await paid_order(world.database)
    world.clock.advance(SHIPMENT_FALLBACK_GRACE + timedelta(seconds=1))

    with structlog.testing.capture_logs() as logs:
        await ship_unshipped_once(world.fallback, world.metrics)

    [log] = logs
    assert (log["event"], log["log_level"], log["error"]) == (
        "shipment_creation_failed",
        "error",
        "RecordNotFound",
    )
    assert log["exc_info"] is not None
    assert world.creations("failed", "fallback") == 1


async def test_the_fallback_ships_what_a_crash_left_behind() -> None:
    world = World()
    order = await paid_order(world.database)
    world.clock.advance(SHIPMENT_FALLBACK_GRACE + timedelta(seconds=1))

    with structlog.testing.capture_logs() as logs:
        await ship_unshipped_once(world.fallback, world.metrics)
        await ship_unshipped_once(world.fallback, world.metrics)

    [log] = logs
    assert (log["event"], log["via"], log["order_id"]) == (
        "shipment_created",
        "fallback",
        str(order.id),
    )
    assert world.creations("created", "fallback") == 1


class DownClaims(InMemoryShipmentRepository):
    error: Exception = ServiceUnavailable("database unavailable: scripted")

    async def unshipped_orders(
        self, *, paid_before: datetime, limit: int
    ) -> tuple[OrderRecord, ...]:
        raise self.error


class BrokenClaims(DownClaims):
    error = RuntimeError("a bug")


@pytest.mark.parametrize(
    ("shipments", "event"),
    [(DownClaims, "shipment_fallback_unavailable"), (BrokenClaims, "shipment_fallback_failed")],
)
async def test_a_failed_claim_is_logged_and_never_raised(
    shipments: type[DownClaims], event: str
) -> None:
    world = World(shipments)

    with structlog.testing.capture_logs() as logs:
        await ship_unshipped_once(world.fallback, world.metrics)

    assert [log["event"] for log in logs] == [event]


async def test_the_loop_ships_at_startup_then_every_interval_until_stopped() -> None:
    world = World()
    await paid_order(world.database)
    world.clock.advance(SHIPMENT_FALLBACK_GRACE + timedelta(seconds=1))
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_shipments(world.fallback, stop, interval_s=0.01, metrics=world.metrics)
    )
    await until(lambda: len(world.database.tables.shipments) == 1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert world.creations("created", "fallback") == 1


# --- The tracking ----------------------------------------------------------------------


def _tracking(world: World, carrier: FakeCarrierProvider | None = None) -> SyncShipmentTracking:
    return SyncShipmentTracking(
        world.shipments,
        carrier if carrier is not None else world.carrier,
        world.events,
        world.clock,
        lease=timedelta(seconds=60),
        budget=timedelta(seconds=60),
    )


async def _shipped(world: World, comment: str = "comment-1") -> ShipmentRecord:
    created = await world.ship(await paid_order(world.database, comment=comment))
    assert created.shipment is not None
    return created.shipment


async def test_a_tracking_tick_logs_each_move_and_counts_every_shipment() -> None:
    world = World()
    moving = await _shipped(world, "comment-1")
    await _shipped(world, "comment-2")
    world.carrier.advance(moving.ref, ShipmentStatus.IN_TRANSIT)
    world.carrier.advance(moving.ref, ShipmentStatus.OUT_FOR_DELIVERY)

    with structlog.testing.capture_logs() as logs:
        await track_once(_tracking(world), world.metrics)

    [log] = logs
    order = world.database.tables.orders[moving.order_id]
    assert log == {
        "event": "shipment_moved",
        "log_level": "info",
        "shipment_id": str(moving.id),
        "order_id": str(order.id),
        "reference": order.reference.value,
        "carrier": "mock",
        "outcome": "moved",
        "from_status": "created",
        "status": "out_for_delivery",
        "transitions": 2,
    }
    assert moving.ref.tracking_number not in str(logs)
    assert world.sample("livecart_shipment_tracking_total", outcome="moved") == 1
    assert world.sample("livecart_shipment_tracking_total", outcome="unchanged") == 1
    assert world.sample("livecart_shipment_transitions_total", status="in_transit") == 1
    assert world.sample("livecart_shipment_transitions_total", status="out_for_delivery") == 1
    assert world.sample("livecart_shipment_transitions_total", status="delivered") == 0


async def test_a_tracking_failure_is_an_error_and_an_outage_a_warning() -> None:
    unknown, down = World(), World()
    await _shipped(unknown)
    await _shipped(down)
    down.carrier.fail_next()

    with structlog.testing.capture_logs() as logs:
        await track_once(_tracking(unknown, FakeCarrierProvider(unknown.clock)), unknown.metrics)
        await track_once(_tracking(down), down.metrics)

    assert [(log["event"], log["log_level"], log["error"]) for log in logs] == [
        ("shipment_tracking_failed", "error", "RecordNotFound"),
        ("shipment_tracking_deferred", "warning", "ServiceUnavailable"),
    ]
    assert unknown.sample("livecart_shipment_tracking_total", outcome="failed") == 1
    assert down.sample("livecart_shipment_tracking_total", outcome="retry") == 1


class DownLeases(InMemoryShipmentRepository):
    error: Exception = ServiceUnavailable("database unavailable: scripted")

    async def lease_for_tracking(
        self, *, now: datetime, limit: int, lease: timedelta
    ) -> tuple[LeasedShipment, ...]:
        raise self.error


class BrokenLeases(DownLeases):
    error = RuntimeError("a bug")


@pytest.mark.parametrize(
    ("shipments", "event"),
    [
        (DownLeases, "shipment_tracking_unavailable"),
        (BrokenLeases, "shipment_tracking_tick_failed"),
    ],
)
async def test_a_failed_lease_is_logged_and_never_raised(
    shipments: type[DownLeases], event: str
) -> None:
    world = World(shipments)

    with structlog.testing.capture_logs() as logs:
        await track_once(_tracking(world), world.metrics)

    assert [log["event"] for log in logs] == [event]


async def test_the_loop_ships_then_tracks_at_every_tick() -> None:
    world = World()
    await paid_order(world.database)
    world.clock.advance(SHIPMENT_FALLBACK_GRACE + timedelta(seconds=1))
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_shipments(
            world.fallback, stop, sync=_tracking(world), interval_s=0.01, metrics=world.metrics
        )
    )
    await until(lambda: len(world.database.tables.shipments) == 1)
    [shipment] = world.database.tables.shipments.values()
    world.clock.advance(timedelta(seconds=61))
    world.carrier.advance(shipment.ref, ShipmentStatus.IN_TRANSIT)
    await until(
        lambda: world.database.tables.shipments[shipment.id].status is ShipmentStatus.IN_TRANSIT
    )
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert world.sample("livecart_shipment_transitions_total", status="in_transit") == 1
