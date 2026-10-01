"""The shipments of paid orders on the compose PostgreSQL and Redis, with the mock carrier.

A crash between a payment and its shipment leaves the order to the
fallback, which ships it once, with one ``shipment.updated``; creations of
one order's shipment at once, the payment's path and the fallback on
several workers, record one shipment and publish it once. The tracking then
walks the mock's route, computed from a frozen clock, one transition and
one ``shipment.updated`` at a time, whatever the workers racing on it.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.apply_payment_events import ApplyPaymentEvent, ProcessPaymentEvents
from livecart_core.application.create_shipment import (
    SHIPMENT_FALLBACK_GRACE,
    CreateShipment,
    ShipmentCreationOutcome,
    ShipUnshippedOrders,
)
from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    IntentSource,
    LeasedShipment,
    OrderRecord,
    PaymentEvent,
    PaymentProviderName,
    ReservationRequest,
    SessionEventType,
)
from livecart_core.application.ports import START_CURSOR
from livecart_core.application.sync_shipment_tracking import SyncShipmentTracking, TrackingOutcome
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus, ShipmentStatus
from livecart_core.infrastructure.carriers.mock import MockCarrier
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.shipments import SqlShipmentRepository
from livecart_core.infrastructure.db.webhook_events import SqlWebhookEventRegistry
from livecart_core.infrastructure.events import RedisSessionEvents
from livecart_core.testing.backends import PostgresBackend
from livecart_core.testing.builders import TEST_ADDRESS
from livecart_core.testing.fakes import FakeClock
from livecart_core.testing.keys import TEST_MOCK_CARRIER_SECRET
from livecart_worker.payments import PAYMENT_EVENTS_BUDGET, process_once
from livecart_worker.shipments import ship_unshipped_once

pytestmark = pytest.mark.integration


async def _paid(backend: PostgresBackend, app_engine: AsyncEngine, clock: FakeClock) -> UUID:
    """An order paid through the SQL registry, as the payment loop of a worker pays it."""
    session = await backend.add_live_session()
    await backend.add_product(session.id, "B07", price=Money(1_250, "EUR"))
    comment_id = await backend.add_comment("comment-1")
    now = clock.now()
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
            now=now,
            reserved_until=now + timedelta(seconds=900),
        )
    )
    assert result is not None
    assert result.order is not None
    order = result.order
    await backend.orders.set_shipping_address(order.id, TEST_ADDRESS)
    opening = await backend.payments.open_attempt(
        order.id,
        provider=PaymentProviderName.MOCK,
        return_url="livecart://payment-return",
        now=now,
        grace=timedelta(seconds=600),
        max_attempts=3,
    )
    assert opening.payment is not None
    await backend.payments.record_checkout(
        opening.payment.id, provider_ref="mock_cs_ship", redirect_url="http://localhost:8080/x"
    )
    registry = SqlWebhookEventRegistry(app_engine)
    await registry.record(
        PaymentEvent(
            provider=PaymentProviderName.MOCK,
            event_id="evt_mock_ship",
            event_type=CheckoutEventType.COMPLETED.value,
            checkout_ref="mock_cs_ship",
            payment_ref="mock_pi_ship",
            payment_status=CheckoutPaymentStatus.PAID,
            amount_minor=2_500,
            currency="EUR",
            occurred_at=now,
        ),
        now=now,
    )
    return order.id


async def _shipments(migrator: AsyncEngine) -> int:
    async with migrator.connect() as connection:
        counted = await connection.scalar(select(func.count()).select_from(tables.shipments))
    return int(counted or 0)


async def _order(backend: PostgresBackend, order_id: UUID) -> OrderRecord:
    order = await backend.orders.get(order_id)
    assert order is not None
    return order


async def test_a_payment_a_crash_left_without_its_shipment_ships_by_the_fallback(
    postgres_backend: PostgresBackend,
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
) -> None:
    clock = FakeClock(datetime.now(UTC))
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)
    order_id = await _paid(postgres_backend, app_engine, clock)
    registry = SqlWebhookEventRegistry(app_engine)
    process = ProcessPaymentEvents(
        registry, ApplyPaymentEvent(registry, events, clock), clock, budget=PAYMENT_EVENTS_BUDGET
    )
    # The worker pays the order, then dies before it creates the shipment.
    await process_once(process)
    order = await _order(postgres_backend, order_id)
    assert order.status is OrderStatus.PAID
    assert await _shipments(migrator_engine) == 0

    shipments = SqlShipmentRepository(app_engine)
    ship = CreateShipment(shipments, MockCarrier(TEST_MOCK_CARRIER_SECRET, clock), events, clock)
    fallback = ShipUnshippedOrders(shipments, ship, clock)
    # Within the grace, the fallback leaves the order to the path that paid it.
    await ship_unshipped_once(fallback)
    assert await _shipments(migrator_engine) == 0
    clock.advance(SHIPMENT_FALLBACK_GRACE + timedelta(seconds=1))
    await ship_unshipped_once(fallback)
    await ship_unshipped_once(fallback)

    assert await _shipments(migrator_engine) == 1
    shipment = await shipments.get_by_order(order_id)
    assert shipment is not None
    assert shipment.status is ShipmentStatus.CREATED
    streamed = await events.read(
        order.session_id, after=START_CURSOR, max_events=10, block_seconds=0
    )
    assert [item.event.type for item in streamed] == [
        SessionEventType.ORDER_UPDATED,
        SessionEventType.STOCK_UPDATED,
        SessionEventType.SHIPMENT_UPDATED,
    ]
    assert streamed[2].event.data["shipment_id"] == str(shipment.id)
    assert streamed[2].event.data["status"] == "created"


async def test_creations_at_once_record_one_shipment_and_publish_it_once(
    postgres_backend: PostgresBackend,
    worker_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
) -> None:
    clock = FakeClock(datetime.now(UTC))
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)
    order_id = await _paid(postgres_backend, worker_engine, clock)
    registry = SqlWebhookEventRegistry(worker_engine)
    await process_once(
        ProcessPaymentEvents(
            registry,
            ApplyPaymentEvent(registry, events, clock),
            clock,
            budget=PAYMENT_EVENTS_BUDGET,
        )
    )
    order = await _order(postgres_backend, order_id)
    # One carrier per worker: they share nothing but the key, as the mock needs.
    creations = [
        CreateShipment(
            SqlShipmentRepository(worker_engine),
            MockCarrier(TEST_MOCK_CARRIER_SECRET, clock),
            events,
            clock,
        )
        for _ in range(12)
    ]

    created = await asyncio.gather(*(create(order) for create in creations))

    outcomes = [item.outcome for item in created]
    assert outcomes.count(ShipmentCreationOutcome.CREATED) == 1
    assert outcomes.count(ShipmentCreationOutcome.EXISTING) == 11
    assert len({item.shipment for item in created}) == 1
    assert await _shipments(migrator_engine) == 1
    streamed = await events.read(
        order.session_id, after=START_CURSOR, max_events=10, block_seconds=0
    )
    shipped = [item for item in streamed if item.event.type is SessionEventType.SHIPMENT_UPDATED]
    assert len(shipped) == 1


# --- The tracking ----------------------------------------------------------------------

STEP = timedelta(seconds=120)
INTERVAL = timedelta(seconds=60)


async def _shipped(
    backend: PostgresBackend,
    engine: AsyncEngine,
    clock: FakeClock,
    events: RedisSessionEvents,
) -> tuple[OrderRecord, UUID]:
    """A paid order and its shipment, created by the mock carrier at the clock's time."""
    order_id = await _paid(backend, engine, clock)
    registry = SqlWebhookEventRegistry(engine)
    await process_once(
        ProcessPaymentEvents(
            registry,
            ApplyPaymentEvent(registry, events, clock),
            clock,
            budget=PAYMENT_EVENTS_BUDGET,
        )
    )
    order = await _order(backend, order_id)
    carrier = MockCarrier(TEST_MOCK_CARRIER_SECRET, clock, step=STEP)
    created = await CreateShipment(SqlShipmentRepository(engine), carrier, events, clock)(order)
    assert created.shipment is not None
    return order, created.shipment.id


def _tracking(
    shipments: SqlShipmentRepository, clock: FakeClock, events: RedisSessionEvents
) -> SyncShipmentTracking:
    return SyncShipmentTracking(
        shipments,
        MockCarrier(TEST_MOCK_CARRIER_SECRET, clock, step=STEP),
        events,
        clock,
        lease=INTERVAL,
        budget=INTERVAL,
    )


async def _shipment_events(
    events: RedisSessionEvents, order: OrderRecord
) -> list[tuple[str, object]]:
    streamed = await events.read(
        order.session_id, after=START_CURSOR, max_events=50, block_seconds=0
    )
    return [
        (str(item.event.data["status"]), item.event.data["last_event_at"])
        for item in streamed
        if item.event.type is SessionEventType.SHIPMENT_UPDATED
    ]


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def test_the_mock_carriers_route_reaches_the_stream_one_transition_at_a_time(
    postgres_backend: PostgresBackend,
    app_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
) -> None:
    clock = FakeClock(datetime.now(UTC))
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)
    order, shipment_id = await _shipped(postgres_backend, app_engine, clock, events)
    shipments = SqlShipmentRepository(app_engine)
    tracking = _tracking(shipments, clock, events)
    created_at = clock.now()

    outcomes = [(await tracking()).count(TrackingOutcome.UNCHANGED)]
    # The clock stands still between the ticks: each tick finds one step more.
    for _ in range(3):
        clock.advance(STEP)
        outcomes.append((await tracking()).count(TrackingOutcome.MOVED))
    clock.advance(STEP)
    after = await tracking()

    assert outcomes == [1, 1, 1, 1]
    assert after.tracked == ()
    assert await _shipment_events(events, order) == [
        ("created", _iso(created_at)),
        ("in_transit", _iso(created_at + STEP)),
        ("out_for_delivery", _iso(created_at + 2 * STEP)),
        ("delivered", _iso(created_at + 3 * STEP)),
    ]
    stored = await shipments.get_by_order(order.id)
    assert stored is not None
    assert (stored.id, stored.status) == (shipment_id, ShipmentStatus.DELIVERED)
    assert stored.last_event_at == created_at + 3 * STEP


class RacingLeases(SqlShipmentRepository):
    """Leases taken one after the other, then held until every worker has its own.

    Each worker's clock is an interval past the one before, as workers whose
    leases all ended would be: every one of them follows the same shipment,
    and only the compare-and-set keeps their moves apart.
    """

    def __init__(self, engine: AsyncEngine, barrier: asyncio.Barrier, lock: asyncio.Lock) -> None:
        super().__init__(engine)
        self._barrier = barrier
        self._lock = lock

    async def lease_for_tracking(
        self, *, now: datetime, limit: int, lease: timedelta
    ) -> tuple[LeasedShipment, ...]:
        async with self._lock:
            leased = await super().lease_for_tracking(now=now, limit=limit, lease=lease)
        await self._barrier.wait()
        return leased


async def test_workers_racing_on_one_shipment_publish_one_update_per_transition(
    postgres_backend: PostgresBackend,
    worker_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
) -> None:
    workers = 4
    clock = FakeClock(datetime.now(UTC))
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)
    order, shipment_id = await _shipped(postgres_backend, worker_engine, clock, events)
    barrier, lock = asyncio.Barrier(workers), asyncio.Lock()
    # Delivered by every worker's clock: each one walks the whole route.
    delivered = clock.now() + 3 * STEP
    clocks = [FakeClock(delivered + n * (INTERVAL + timedelta(seconds=1))) for n in range(workers)]
    trackings = [
        _tracking(RacingLeases(worker_engine, barrier, lock), worker_clock, events)
        for worker_clock in clocks
    ]

    ticks = await asyncio.gather(*(tracking() for tracking in trackings))

    followed = [tracked for tick in ticks for tracked in tick.tracked]
    assert len(followed) == workers
    moved = sorted(move.status for tracked in followed for move in tracked.moves)
    assert moved == sorted(
        [ShipmentStatus.IN_TRANSIT, ShipmentStatus.OUT_FOR_DELIVERY, ShipmentStatus.DELIVERED]
    )
    published = sorted(status for status, _ in await _shipment_events(events, order))
    assert published == ["created", "delivered", "in_transit", "out_for_delivery"]
    stored = await SqlShipmentRepository(worker_engine).get_by_order(order.id)
    assert stored is not None
    assert (stored.id, stored.status) == (shipment_id, ShipmentStatus.DELIVERED)
