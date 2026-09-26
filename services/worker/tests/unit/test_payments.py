"""The payment events loop over the in-memory fakes: its logs, its metrics, its ticks."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import structlog

from livecart_core.application.apply_payment_events import (
    PAYMENT_EVENT_LEASE,
    ApplyPaymentEvent,
    ProcessPaymentEvents,
)
from livecart_core.application.dto import (
    CheckoutEventType,
    EventClaim,
    IntentSource,
    PaymentEvent,
    PaymentProviderName,
    PaymentSettlement,
    ReservationRequest,
)
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
    InMemoryDatabase,
    InMemoryWebhookEventRegistry,
)
from livecart_core.testing.polling import until
from livecart_worker.metrics import WorkerMetrics
from livecart_worker.payments import PAYMENT_EVENTS_BUDGET, process_once, run_payment_events

TTL = timedelta(seconds=900)
REF = "mock_cs_1"


async def _order(database: InMemoryDatabase, clock: FakeClock) -> None:
    """A pending order of 2 x B07 at 12.50 EUR, its attempt open on the session REF."""
    session = database.add_live_session()
    database.add_product(session.id, "B07", price=Money(1_250, "EUR"))
    claim = await database.comments.claim(
        queued_comment(), holder="w/1-0", now=clock.now(), lease=timedelta(seconds=30)
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
            now=clock.now(),
            reserved_until=clock.now() + TTL,
        )
    )
    assert result is not None
    assert result.order is not None
    opening = await database.payments.open_attempt(
        result.order.id,
        provider=PaymentProviderName.MOCK,
        return_url="livecart://payment-return",
        now=clock.now(),
        grace=timedelta(seconds=600),
        max_attempts=3,
    )
    assert opening.payment is not None
    await database.payments.record_checkout(
        opening.payment.id, provider_ref=REF, redirect_url="https://pay.invalid/1"
    )


def _event(clock: FakeClock, event_id: str = "evt_1", /, **fields: object) -> PaymentEvent:
    values: dict[str, object] = {
        "provider": PaymentProviderName.MOCK,
        "event_id": event_id,
        "event_type": CheckoutEventType.COMPLETED.value,
        "checkout_ref": REF,
        "payment_ref": "mock_pi_1",
        "amount_minor": 2_500,
        "currency": "EUR",
        "occurred_at": clock.now(),
    }
    values.update(fields)
    return PaymentEvent.model_validate(values)


def _process(
    registry: InMemoryWebhookEventRegistry, clock: FakeClock, events: FakeEventPublisher
) -> ProcessPaymentEvents:
    return ProcessPaymentEvents(
        registry, ApplyPaymentEvent(registry, events, clock), clock, budget=PAYMENT_EVENTS_BUDGET
    )


class World:
    def __init__(self, registry: type[InMemoryWebhookEventRegistry] = InMemoryWebhookEventRegistry):
        self.clock = FakeClock()
        self.database = InMemoryDatabase()
        self.registry = registry(self.database)
        self.events = FakeEventPublisher()
        self.metrics = WorkerMetrics()
        self.process = _process(self.registry, self.clock, self.events)

    async def record(self, **fields: object) -> PaymentEvent:
        event = _event(self.clock, **fields)
        await self.registry.record(event, now=self.clock.now())
        return event

    def sample(self, name: str, **labels: str) -> float | None:
        return self.metrics.registry.get_sample_value(name, labels)

    @property
    def order_status(self) -> OrderStatus:
        [order] = self.database.tables.orders.values()
        return order.status


@pytest.fixture
async def world() -> World:
    built = World()
    await _order(built.database, built.clock)
    return built


async def test_a_paid_order_is_logged_and_counted(world: World) -> None:
    await world.record()

    with structlog.testing.capture_logs() as logs:
        await process_once(world.process, world.metrics)

    assert world.order_status is OrderStatus.PAID
    [log] = logs
    assert (log["event"], log["result"], log["log_level"]) == (
        "payment_event_handled",
        "applied",
        "info",
    )
    assert (log["event_id"], log["order_status"], log["attempts"]) == ("evt_1", "paid", 1)
    assert world.sample("livecart_payment_events_total", result="applied") == 1
    assert world.sample("livecart_payment_events_total", result="amount_mismatch") == 0
    assert world.sample("livecart_orders_paid_total") == 1
    assert world.sample("livecart_orders_paid_after_expiry_total") == 0


async def test_a_wrong_amount_is_an_error_to_look_into(world: World) -> None:
    await world.record(amount_minor=100)

    with structlog.testing.capture_logs() as logs:
        await process_once(world.process, world.metrics)

    [log] = logs
    assert (log["event"], log["log_level"]) == ("payment_event_amount_mismatch", "error")
    assert (log["charged_minor"], log["charged_currency"]) == (100, "EUR")
    assert world.order_status is OrderStatus.PENDING_PAYMENT
    assert world.sample("livecart_payment_events_total", result="amount_mismatch") == 1
    assert world.sample("livecart_orders_paid_total") == 0


async def test_a_payment_after_expiry_is_an_error_and_a_refund_to_count(world: World) -> None:
    [payment] = world.database.tables.payments.values()
    await world.database.payments.set_status(payment.id, PaymentStatus.EXPIRED)
    world.clock.advance(TTL + timedelta(seconds=1))
    await world.database.orders.expire_due(now=world.clock.now(), limit=10)
    await world.record()

    with structlog.testing.capture_logs() as logs:
        await process_once(world.process, world.metrics)

    [log] = logs
    assert (log["event"], log["log_level"]) == ("payment_after_expiry", "error")
    assert log["order_status"] == "paid_after_expiry"
    assert world.sample("livecart_orders_paid_after_expiry_total") == 1
    assert world.sample("livecart_orders_paid_total") == 0


class FailingSettlements(InMemoryWebhookEventRegistry):
    async def settle(
        self, event: PaymentEvent, effect: PaymentEffect, *, now: datetime
    ) -> PaymentSettlement | None:
        raise ServiceUnavailable("database unavailable: scripted")


async def test_failed_attempts_are_warnings_and_the_last_one_an_error() -> None:
    world = World(FailingSettlements)
    await _order(world.database, world.clock)
    await world.record()

    with structlog.testing.capture_logs() as logs:
        for _ in range(10):
            await process_once(world.process, world.metrics)
            world.clock.advance(PAYMENT_EVENT_LEASE + timedelta(seconds=1))

    assert [(log["event"], log["log_level"]) for log in logs] == [
        ("payment_event_attempt_failed", "warning")
    ] * 9 + [("payment_event_failed", "error")]
    assert (logs[-1]["attempts"], logs[-1]["error"]) == (10, "ServiceUnavailable")
    assert world.sample("livecart_payment_events_total", result="retry") == 9
    assert world.sample("livecart_payment_events_total", result="attempts_exhausted") == 1


class BrokenSettlements(InMemoryWebhookEventRegistry):
    async def settle(
        self, event: PaymentEvent, effect: PaymentEffect, *, now: datetime
    ) -> PaymentSettlement | None:
        raise RuntimeError("a bug")


async def test_an_attempt_that_hits_a_bug_is_logged_with_its_traceback() -> None:
    world = World(BrokenSettlements)
    await _order(world.database, world.clock)
    await world.record()

    with structlog.testing.capture_logs() as logs:
        await process_once(world.process, world.metrics)

    [log] = logs
    assert (log["event"], log["error"]) == ("payment_event_attempt_failed", "RuntimeError")
    assert isinstance(log["exc_info"], RuntimeError)


async def test_an_event_of_no_attempt_is_a_warning(world: World) -> None:
    await world.record(checkout_ref="cs_of_another_integration")

    with structlog.testing.capture_logs() as logs:
        await process_once(world.process, world.metrics)

    [log] = logs
    assert (log["event"], log["log_level"]) == ("payment_event_unknown_session", "warning")
    assert log["checkout_ref"] == "cs_of_another_integration"


class DownClaims(InMemoryWebhookEventRegistry):
    error: Exception = ServiceUnavailable("database unavailable: scripted")

    async def claim(
        self, *, now: datetime, limit: int, lease: timedelta, max_attempts: int
    ) -> EventClaim:
        raise self.error


class BrokenClaims(DownClaims):
    error = RuntimeError("a bug")


@pytest.mark.parametrize(
    ("registry", "event"),
    [(DownClaims, "payment_events_unavailable"), (BrokenClaims, "payment_events_failed")],
)
async def test_a_failed_tick_is_logged_and_never_raised(
    registry: type[InMemoryWebhookEventRegistry], event: str
) -> None:
    world = World(registry)

    with structlog.testing.capture_logs() as logs:
        await process_once(world.process, world.metrics)

    assert [log["event"] for log in logs] == [event]


async def test_the_loop_applies_at_startup_then_every_interval_until_stopped(world: World) -> None:
    await world.record()
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_payment_events(world.process, stop, interval_s=0.01, metrics=world.metrics)
    )
    await until(lambda: world.order_status is OrderStatus.PAID)
    await world.record(event_id="evt_2", event_type=CheckoutEventType.ASYNC_PAYMENT_SUCCEEDED.value)
    await until(lambda: world.sample("livecart_payment_events_total", result="replayed") == 1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
