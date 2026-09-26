"""The expiry sweep loop over the in-memory repositories."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import structlog

from livecart_core.application.dto import (
    ExpiredReservations,
    IntentSource,
    ReservationRequest,
)
from livecart_core.application.expire_reservations import ExpireReservations
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import (
    FakeClock,
    FakeEventPublisher,
    InMemoryDatabase,
    InMemoryOrderRepository,
)
from livecart_core.testing.polling import until
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
