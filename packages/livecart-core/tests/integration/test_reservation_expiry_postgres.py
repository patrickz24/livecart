"""The expiry sweep on PostgreSQL: an unpaid reservation expires, and two sweeps never double.

The reservation lasts 2 s, which the settings would refuse (a minute at
least, for a buyer to pay from a phone): the use cases take any positive
TTL, and the clock is injected, so the test moves time rather than waits.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import (
    IntentSource,
    ReservationRequest,
    SessionEventType,
    StockLevel,
)
from livecart_core.application.expire_reservations import ExpireReservations
from livecart_core.application.process_comment import Handling, ProcessComment
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.outcomes import IntentOutcome
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.testing.backends import ORDER_TOKENS, PostgresBackend
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, queued_comment
from livecart_core.testing.fakes import (
    FakeBuyerNotifier,
    FakeClock,
    FakeCommentQueue,
    FakeEventPublisher,
)

pytestmark = pytest.mark.integration

RESERVATION_TTL = timedelta(seconds=2)
BUDGET = timedelta(seconds=5)


async def _stock(backend: PostgresBackend, product_id: UUID) -> StockLevel:
    product = await backend.products.get(product_id)
    assert product is not None
    return product.stock


async def test_an_unpaid_reservation_expires_and_its_stock_returns(
    postgres_backend: PostgresBackend,
) -> None:
    session = await postgres_backend.add_live_session()
    product = await postgres_backend.add_product(session.id, "B07", stock_total=1)
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    events = FakeEventPublisher()
    process = ProcessComment(
        inbox=postgres_backend.comments,
        sessions=postgres_backend.live_sessions,
        orders=postgres_backend.orders,
        queue=queue,
        events=events,
        notifier=FakeBuyerNotifier(),
        access_token=ORDER_TOKENS.token_for,
        clock=clock,
        lease=timedelta(seconds=30),
        reservation_ttl=RESERVATION_TTL,
        max_quantity=DEMO_MAX_QUANTITY,
    )
    sweep = ExpireReservations(postgres_backend.orders, events, clock, budget=BUDGET)
    first = queued_comment("je prends B07", external_comment_id="c-1", author="buyer-1")
    second = queued_comment("je prends B07", external_comment_id="c-2", author="buyer-2")
    await queue.enqueue([first, second])
    first_delivery, second_delivery = await queue.receive(
        "worker-1", max_messages=2, block_seconds=0
    )

    held = await process(first_delivery, consumer="worker-1")
    assert held.reservation is not None
    order = held.reservation.order
    assert order is not None
    assert order.reserved_until == clock.now() + RESERVATION_TTL
    assert await _stock(postgres_backend, product.id) == StockLevel(1, reserved=1)

    # At its very end the reservation still holds: expired only once past it.
    clock.advance(RESERVATION_TTL)
    assert (await sweep()).expired == 0
    clock.advance(1)
    events.published.clear()
    swept = await sweep()

    assert swept.expired == 1
    expired = await postgres_backend.orders.get(order.id)
    assert expired is not None
    assert (expired.status, expired.expired_at) == (OrderStatus.EXPIRED, clock.now())
    assert await _stock(postgres_backend, product.id) == StockLevel(1)
    assert [event.type for event in events.published] == [
        SessionEventType.ORDER_UPDATED,
        SessionEventType.STOCK_UPDATED,
    ]
    # The unit is for sale again.
    next_buyer = await process(second_delivery, consumer="worker-1")
    assert next_buyer.handling is Handling.PROCESSED
    assert next_buyer.reservation is not None
    assert next_buyer.reservation.intent.outcome is IntentOutcome.RESERVED


async def _open_orders(
    backend: PostgresBackend, session_id: UUID, codes: list[str], *, count: int, now: FakeClock
) -> None:
    """``count`` pending orders spread over ``codes``, each of a buyer of its own."""
    comments = [(new_id(), f"c-{n}", f"buyer-{n}") for n in range(count)]
    await backend.add_comments(comments)
    for n, (comment_id, _, _) in enumerate(comments):
        code = codes[n % len(codes)]
        result = await backend.orders.reserve(
            ReservationRequest(
                comment_id=comment_id,
                session_id=session_id,
                product_code=ProductCode(code),
                variant=Variant(),
                quantity=Quantity(1),
                source=IntentSource.PARSER,
                confidence=Decimal("1.00"),
                evidence=("je prends", code),
                now=now.now(),
                reserved_until=now.now() + RESERVATION_TTL,
            )
        )
        assert result is not None
        assert result.intent.outcome is IntentOutcome.RESERVED


async def test_two_concurrent_sweeps_release_each_order_exactly_once(
    postgres_backend: PostgresBackend, migrator_engine: AsyncEngine
) -> None:
    session = await postgres_backend.add_live_session()
    codes = ["B07", "C3", "Z99"]
    products = [
        await postgres_backend.add_product(session.id, code, stock_total=100) for code in codes
    ]
    clock = FakeClock()
    await _open_orders(postgres_backend, session.id, codes, count=250, now=clock)
    clock.advance(RESERVATION_TTL + timedelta(seconds=1))
    events = FakeEventPublisher()
    sweeps = [
        ExpireReservations(postgres_backend.orders, events, clock, budget=BUDGET) for _ in range(2)
    ]

    first, second = await asyncio.gather(*(sweep() for sweep in sweeps))

    # Each order expired once: a second release would also have broken
    # ck_products_stock (reserved >= 0) and failed its batch.
    assert first.expired + second.expired == 250
    assert first.batches + second.batches >= 3
    async with migrator_engine.connect() as connection:
        rows = (
            await connection.execute(
                select(tables.orders.c.status, func.count()).group_by(tables.orders.c.status)
            )
        ).all()
    statuses = {row[0]: row[1] for row in rows}
    assert statuses == {OrderStatus.EXPIRED.value: 250}
    for product in products:
        assert await _stock(postgres_backend, product.id) == StockLevel(100)
    order_events = [
        event.data["order_id"] for event in events.of_type(SessionEventType.ORDER_UPDATED)
    ]
    assert len(order_events) == len(set(order_events)) == 250
