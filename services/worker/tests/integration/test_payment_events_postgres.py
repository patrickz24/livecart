"""The payment events loop on the compose PostgreSQL: durable events, applied once, or failed.

The first test runs the whole worker (``serve``, with Redis for its other
loops and the session streams), stops it, lets the webhook record a payment
while it is down, and starts it again: the event waited in the registry, and
the worker applies it at once. The second drives the loop's ticks on the SQL
registry with a settlement that always fails: the tenth attempt, counted by
the registry, fails the event for good.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.apply_payment_events import (
    PAYMENT_EVENT_LEASE,
    ApplyPaymentEvent,
    ProcessPaymentEvents,
)
from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    IntentSource,
    OrderRecord,
    PaymentEvent,
    PaymentProviderName,
    PaymentSettlement,
    ReservationRequest,
    SessionEventType,
    StockLevel,
)
from livecart_core.application.ports import START_CURSOR, ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.payments import PaymentEffect
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.webhook_events import SqlWebhookEventRegistry
from livecart_core.infrastructure.events import RedisSessionEvents
from livecart_core.settings import Settings
from livecart_core.testing.backends import PostgresBackend
from livecart_core.testing.database import IntegrationSettings
from livecart_core.testing.fakes import FakeClock, FakeEventPublisher
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY
from livecart_core.testing.polling import until
from livecart_worker.main import serve
from livecart_worker.metrics import WorkerMetrics
from livecart_worker.payments import PAYMENT_EVENTS_BUDGET, process_once

pytestmark = pytest.mark.integration

REF = "mock_cs_restart"


async def _checkout(backend: PostgresBackend) -> tuple[OrderRecord, Any]:
    """A pending order of 2 x B07 at 12.50 EUR, its attempt open on the session REF."""
    session = await backend.add_live_session()
    product = await backend.add_product(session.id, "B07", price=Money(1_250, "EUR"))
    comment_id = await backend.add_comment("comment-1")
    now = datetime.now(UTC)
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
    opening = await backend.payments.open_attempt(
        result.order.id,
        provider=PaymentProviderName.MOCK,
        return_url="livecart://payment-return",
        now=now,
        grace=timedelta(seconds=600),
        max_attempts=3,
    )
    assert opening.payment is not None
    await backend.payments.record_checkout(
        opening.payment.id, provider_ref=REF, redirect_url=f"http://localhost:8080/{REF}"
    )
    return result.order, product.id


def _completed(occurred_at: datetime) -> PaymentEvent:
    return PaymentEvent(
        provider=PaymentProviderName.MOCK,
        event_id="evt_mock_restart",
        event_type=CheckoutEventType.COMPLETED.value,
        checkout_ref=REF,
        payment_ref="mock_pi_restart",
        payment_status=CheckoutPaymentStatus.PAID,
        amount_minor=2_500,
        currency="EUR",
        occurred_at=occurred_at,
    )


async def _row(migrator: AsyncEngine, event: PaymentEvent) -> dict[str, Any]:
    events = tables.processed_webhook_events
    async with migrator.connect() as connection:
        row = (
            await connection.execute(select(events).where(events.c.event_id == event.event_id))
        ).one()
    return dict(row._mapping)


async def test_an_event_recorded_while_the_worker_is_stopped_is_applied_at_restart(
    integration_settings: IntegrationSettings,
    postgres_backend: PostgresBackend,
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
    tmp_path: Path,
) -> None:
    order, product_id = await _checkout(postgres_backend)
    settings = Settings(
        database_url=integration_settings.database_url,
        redis_url=integration_settings.redis_url,
        redis_key_prefix=redis_key_prefix,
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        worker_heartbeat_file=tmp_path / "worker.heartbeat",
        metrics_port=None,
        _env_file=None,
    )
    # The worker runs, then stops: nothing is waiting in the registry yet.
    stop = asyncio.Event()
    running = asyncio.create_task(serve(settings, stop, consumer_name="worker-payments-1"))
    await until(lambda: (tmp_path / "worker.heartbeat").exists(), within_s=15, every_s=0.05)
    stop.set()
    await asyncio.wait_for(running, timeout=35)

    # The provider's webhook arrives while no worker runs: recorded, and it waits.
    event = _completed(datetime.now(UTC))
    registry = SqlWebhookEventRegistry(app_engine)
    assert await registry.record(event, now=datetime.now(UTC)) is True
    await asyncio.sleep(0.5)
    waiting = await _row(migrator_engine, event)
    assert (waiting["attempts"], waiting["processed_at"]) == (0, None)
    assert (await postgres_backend.orders.get(order.id)) == order

    stop = asyncio.Event()
    restarted = asyncio.create_task(serve(settings, stop, consumer_name="worker-payments-2"))

    async def paid() -> bool:
        current = await postgres_backend.orders.get(order.id)
        return current is not None and current.status is OrderStatus.PAID

    await until(paid, within_s=15, every_s=0.05)
    stop.set()
    await asyncio.wait_for(restarted, timeout=35)

    applied = await _row(migrator_engine, event)
    assert (applied["attempts"], applied["outcome"]) == (1, "applied")
    product = await postgres_backend.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10, reserved=0, sold=2)
    attempt = await postgres_backend.payments.latest(order.id)
    assert attempt is not None
    assert (attempt.status, attempt.provider_payment_ref) == (
        PaymentStatus.SUCCEEDED,
        "mock_pi_restart",
    )
    streamed = await RedisSessionEvents(redis_client, prefix=redis_key_prefix).read(
        order.session_id, after=START_CURSOR, max_events=10, block_seconds=0
    )
    assert [item.event.type for item in streamed] == [
        SessionEventType.ORDER_UPDATED,
        SessionEventType.STOCK_UPDATED,
    ]
    assert streamed[0].event.data["status"] == "paid"


class FailingSettlements(SqlWebhookEventRegistry):
    """The SQL registry, whose every settlement fails as a database that keeps timing out."""

    async def settle(
        self, event: PaymentEvent, effect: PaymentEffect, *, now: datetime
    ) -> PaymentSettlement | None:
        raise ServiceUnavailable("database unavailable: statement timeout")


async def test_an_event_fails_for_good_after_ten_attempts_on_postgresql(
    postgres_backend: PostgresBackend,
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
) -> None:
    order, _ = await _checkout(postgres_backend)
    clock = FakeClock(datetime.now(UTC))
    registry = FailingSettlements(app_engine)
    process = ProcessPaymentEvents(
        registry,
        ApplyPaymentEvent(registry, FakeEventPublisher(), clock),
        clock,
        budget=PAYMENT_EVENTS_BUDGET,
    )
    metrics = WorkerMetrics()
    event = _completed(clock.now())
    await registry.record(event, now=clock.now())

    attempts = []
    with structlog.testing.capture_logs() as logs:
        for _ in range(11):
            await process_once(process, metrics)
            attempts.append((await _row(migrator_engine, event))["attempts"])
            clock.advance(PAYMENT_EVENT_LEASE + timedelta(seconds=1))

    assert attempts == [*range(1, 11), 10]
    failed = await _row(migrator_engine, event)
    assert (failed["outcome"], failed["attempts"]) == ("failed", 10)
    assert failed["processed_at"] is not None
    assert [log["event"] for log in logs] == ["payment_event_attempt_failed"] * 9 + [
        "payment_event_failed"
    ]
    assert logs[-1]["log_level"] == "error"
    sample = metrics.registry.get_sample_value
    assert sample("livecart_payment_events_total", {"result": "attempts_exhausted"}) == 1
    assert sample("livecart_payment_events_total", {"result": "retry"}) == 9
    assert (await postgres_backend.orders.get(order.id)) == order
