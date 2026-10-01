"""The shipments of paid orders on the compose PostgreSQL and Redis, with the mock carrier.

A crash between a payment and its shipment leaves the order to the
fallback, which ships it once, with one ``shipment.updated``; creations of
one order's shipment at once, the payment's path and the fallback on
several workers, record one shipment and publish it once.
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
    OrderRecord,
    PaymentEvent,
    PaymentProviderName,
    ReservationRequest,
    SessionEventType,
)
from livecart_core.application.ports import START_CURSOR
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
