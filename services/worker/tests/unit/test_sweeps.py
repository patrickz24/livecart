"""The expiry sweep loop over the in-memory repositories: its three phases, logs and metrics."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import structlog

from livecart_core.application.apply_payment_events import ApplyPaymentEvent
from livecart_core.application.checkout import CheckoutPages
from livecart_core.application.dto import (
    DueCheckout,
    ExpiredReservations,
    IntentSource,
    PaymentProviderName,
    ReservationRequest,
)
from livecart_core.application.expire_checkouts import ExpireCheckouts
from livecart_core.application.expire_reservations import ExpireReservations
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import (
    FakeClock,
    FakeEventPublisher,
    FakePaymentProvider,
    InMemoryDatabase,
    InMemoryOrderRepository,
)
from livecart_core.testing.polling import until
from livecart_worker.metrics import WorkerMetrics
from livecart_worker.sweeps import EXPIRY_BUDGET, run_expiry_sweep, sweep_once

TTL = timedelta(seconds=900)


async def _due_order(database: InMemoryDatabase, clock: FakeClock) -> None:
    session = database.add_live_session()
    database.add_product(session.id, "B07")
    claim = await database.comments.claim(
        queued_comment(), holder="w/1-0", now=clock.now(), lease=timedelta(seconds=30)
    )
    await database.orders.reserve(
        ReservationRequest(
            comment_id=claim.comment.id,
            session_id=session.id,
            product_code=ProductCode("B07"),
            variant=Variant(),
            quantity=Quantity(1),
            source=IntentSource.PARSER,
            confidence=Decimal("1.00"),
            evidence=("je prends", "B07"),
            now=clock.now(),
            reserved_until=clock.now() + TTL,
        )
    )
    clock.advance(TTL + timedelta(seconds=1))


def _sweep(orders: InMemoryOrderRepository, clock: FakeClock) -> ExpireReservations:
    return ExpireReservations(orders, FakeEventPublisher(), clock, budget=EXPIRY_BUDGET)


async def test_a_tick_that_expires_orders_logs_how_many() -> None:
    database, clock = InMemoryDatabase(), FakeClock()
    await _due_order(database, clock)

    with structlog.testing.capture_logs() as logs:
        await sweep_once(_sweep(database.orders, clock))
        await sweep_once(_sweep(database.orders, clock))

    assert [(log["event"], log["expired"], log["batches"]) for log in logs] == [
        ("reservations_expired", 1, 1)
    ]


class _Failing(InMemoryOrderRepository):
    error: Exception = ServiceUnavailable("database unavailable: scripted")

    async def expire_due(self, *, now: datetime, limit: int) -> ExpiredReservations:
        raise self.error


class _Broken(_Failing):
    error = RuntimeError("a bug")


@pytest.mark.parametrize(
    ("orders", "event"),
    [(_Failing, "reservation_sweep_unavailable"), (_Broken, "reservation_sweep_failed")],
)
async def test_a_failed_tick_is_logged_and_never_raised(orders: type[_Failing], event: str) -> None:
    database, clock = InMemoryDatabase(), FakeClock()

    with structlog.testing.capture_logs() as logs:
        await sweep_once(_sweep(orders(database), clock))

    assert [log["event"] for log in logs] == [event]


async def test_the_loop_sweeps_at_startup_then_every_interval_until_stopped() -> None:
    database, clock = InMemoryDatabase(), FakeClock()
    await _due_order(database, clock)
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_expiry_sweep(_sweep(database.orders, clock), stop, interval_s=0.01)
    )
    await until(
        lambda: {order.status for order in database.tables.orders.values()} == {OrderStatus.EXPIRED}
    )
    stop.set()
    await asyncio.wait_for(task, timeout=2)


# --- The later phases: the orders with a payment in flight -----------------------------


class Checkouts:
    """A due order whose buyer opened the hosted page, the provider, and the sweep's phases."""

    def __init__(self, orders: InMemoryOrderRepository | None = None) -> None:
        self.database, self.clock = InMemoryDatabase(), FakeClock()
        self.provider = FakePaymentProvider(PaymentProviderName.MOCK)
        self.metrics = WorkerMetrics()
        events = FakeEventPublisher()
        registry = self.database.webhook_events
        self.expire = _sweep(self.database.orders, self.clock)
        self.checkouts = ExpireCheckouts(
            orders if orders is not None else self.database.orders,
            self.database.payments,
            self.provider,
            registry,
            ApplyPaymentEvent(registry, events, self.clock),
            events,
            self.clock,
            pages=CheckoutPages("http://localhost:8080"),
            budget=EXPIRY_BUDGET,
        )

    async def due(self, *, provider_ref: str | None = None) -> None:
        await _due_order(self.database, self.clock)
        [order] = self.database.tables.orders.values()
        # Opened before the reservation ended, its grace ended too.
        opening = await self.database.payments.open_attempt(
            order.id,
            provider=PaymentProviderName.MOCK,
            return_url="livecart://payment-return",
            now=order.reserved_until - timedelta(seconds=601),
            grace=timedelta(seconds=600),
            max_attempts=3,
        )
        assert opening.payment is not None
        if provider_ref is None:
            session = await self.provider.create_checkout(
                CheckoutPages("http://localhost:8080").request_for(order.reference, opening.payment)
            )
            provider_ref = session.provider_ref
        await self.database.payments.record_checkout(
            opening.payment.id, provider_ref=provider_ref, redirect_url="https://pay.invalid/1"
        )

    async def tick(self) -> None:
        await sweep_once(self.expire, self.metrics, self.checkouts)

    def sample(self, name: str, **labels: str) -> float | None:
        return self.metrics.registry.get_sample_value(name, labels)


async def test_a_tick_expires_the_session_of_a_due_payment_then_releases_it() -> None:
    shop = Checkouts()
    await shop.due()

    with structlog.testing.capture_logs() as logs:
        await shop.tick()

    [settled] = [log for log in logs if log["event"] == "checkout_expiry_settled"]
    assert (settled["outcome"], settled["order_status"], settled["recreated"]) == (
        "expired",
        "expired",
        False,
    )
    assert "provider_ref" not in settled
    assert shop.sample("livecart_checkout_expiries_total", outcome="expired") == 1
    assert shop.sample("livecart_checkout_expiries_total", outcome="failed") == 0
    assert shop.sample("livecart_reservations_expired_total") == 1
    assert {order.status for order in shop.database.tables.orders.values()} == {OrderStatus.EXPIRED}


async def test_a_session_the_buyer_paid_first_counts_as_an_order_paid() -> None:
    shop = Checkouts()
    await shop.due()
    [payment] = shop.database.tables.payments.values()
    assert payment.provider_ref is not None
    shop.provider.complete(payment.provider_ref)

    await shop.tick()

    assert shop.sample("livecart_checkout_expiries_total", outcome="reconciled") == 1
    assert shop.sample("livecart_orders_paid_total") == 1
    assert shop.sample("livecart_reservations_expired_total") == 0


async def test_what_needs_a_person_is_logged_at_error_and_a_retry_as_a_warning() -> None:
    unknown, down = Checkouts(), Checkouts()
    await unknown.due(provider_ref="mock_cs_unknown")
    await down.due()
    down.provider.fail_next(1)

    with structlog.testing.capture_logs() as logs:
        await unknown.tick()
        await down.tick()

    checkouts = [log for log in logs if log["event"].startswith("checkout_expiry")]
    assert [(log["event"], log["log_level"], log["error"]) for log in checkouts] == [
        ("checkout_expiry_failed", "error", "RecordNotFound"),
        ("checkout_expiry_deferred", "warning", "ServiceUnavailable"),
    ]
    assert unknown.sample("livecart_checkout_expiries_total", outcome="failed") == 1
    assert down.sample("livecart_checkout_expiries_total", outcome="retry") == 1


class _LeaseUnavailable(InMemoryOrderRepository):
    error: Exception = ServiceUnavailable("database unavailable: scripted")

    async def lease_due_checkouts(
        self, *, now: datetime, limit: int, lease: timedelta
    ) -> tuple[DueCheckout, ...]:
        raise self.error


class _LeaseBroken(_LeaseUnavailable):
    error = RuntimeError("a bug")


@pytest.mark.parametrize(
    ("orders", "event"),
    [
        (_LeaseUnavailable, "checkout_expiry_unavailable"),
        (_LeaseBroken, "checkout_expiry_tick_failed"),
    ],
)
async def test_a_failed_lease_is_logged_after_the_first_phase_ran(
    orders: type[_LeaseUnavailable], event: str
) -> None:
    database = InMemoryDatabase()
    shop = Checkouts(orders(database))

    with structlog.testing.capture_logs() as logs:
        await shop.tick()

    assert [log["event"] for log in logs] == [event]
