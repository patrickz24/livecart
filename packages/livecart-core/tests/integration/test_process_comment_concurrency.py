"""ProcessComment under real concurrency, on PostgreSQL through the worker's pool of 20.

Every unit of every delivery runs as livecart_app: the lease (unit 1), the
session resolution, the catalog and unit 3. The deliveries start together
behind an ``asyncio.Barrier``, each for a consumer of its own, as deliveries
spread over several workers would. The tests assert invariants only.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import QueuedComment, QueueDelivery
from livecart_core.application.process_comment import HandledComment, Handling, ProcessComment
from livecart_core.domain.outcomes import IntentOutcome
from livecart_core.domain.state_machines import CommentStatus, OrderStatus
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.engine import WORKER_POOL
from livecart_core.testing.backends import ORDER_TOKENS, PostgresBackend
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, queued_comment
from livecart_core.testing.fakes import (
    FakeBuyerNotifier,
    FakeClock,
    FakeCommentQueue,
    FakeEventPublisher,
)
from livecart_core.testing.pools import PoolProbe

pytestmark = pytest.mark.integration

DEMAND = 1000
STOCK = 200
LEASE = timedelta(seconds=30)
TTL = timedelta(seconds=900)


@dataclass(frozen=True)
class Stored:
    """What the database holds for the product and the session after a race."""

    stock_total: int
    reserved: int
    sold: int
    available: int
    orders: int
    pending_orders: int
    distinct_references: int
    intents: dict[IntentOutcome, int]
    processed_comments: int
    comments: int


async def _setup(
    backend: PostgresBackend, comments: list[QueuedComment], *, stock_total: int
) -> tuple[ProcessComment, FakeCommentQueue, list[QueueDelivery], UUID]:
    """A live session selling Z99, its comments delivered, and the use case."""
    session = await backend.add_live_session()
    await backend.add_product(session.id, "Z99", stock_total=stock_total)
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    await queue.enqueue(comments)
    deliveries = await queue.receive("worker-1", max_messages=len(comments), block_seconds=0)
    process = ProcessComment(
        inbox=backend.comments,
        sessions=backend.live_sessions,
        orders=backend.orders,
        queue=queue,
        events=FakeEventPublisher(),
        notifier=FakeBuyerNotifier(),
        access_token=ORDER_TOKENS.token_for,
        clock=clock,
        lease=LEASE,
        reservation_ttl=TTL,
        max_quantity=DEMO_MAX_QUANTITY,
    )
    return process, queue, deliveries, session.id


async def _race(process: ProcessComment, deliveries: list[QueueDelivery]) -> list[HandledComment]:
    """Every delivery at once: each task waits at the barrier until all are ready."""
    barrier = asyncio.Barrier(len(deliveries))

    async def one(n: int, delivery: QueueDelivery) -> HandledComment:
        await barrier.wait()
        return await process(delivery, consumer=f"worker-{n}")

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(one(n, delivery)) for n, delivery in enumerate(deliveries)]
    return [task.result() for task in tasks]


async def _stored(engine: AsyncEngine, session_id: UUID) -> Stored:
    products, orders = tables.products, tables.orders
    intents, comments = tables.purchase_intents, tables.comments
    async with engine.connect() as connection:
        product = (
            await connection.execute(select(products).where(products.c.session_id == session_id))
        ).one()
        order_counts = (
            await connection.execute(
                select(
                    func.count(),
                    func.count().filter(orders.c.status == OrderStatus.PENDING_PAYMENT.value),
                    func.count(orders.c.reference.distinct()),
                ).where(orders.c.session_id == session_id)
            )
        ).one()
        by_outcome = (
            await connection.execute(
                select(intents.c.outcome, func.count())
                .where(intents.c.session_id == session_id)
                .group_by(intents.c.outcome)
            )
        ).all()
        comment_counts = (
            await connection.execute(
                select(
                    func.count().filter(comments.c.status == CommentStatus.PROCESSED.value),
                    func.count(),
                )
            )
        ).one()
    return Stored(
        stock_total=product.stock_total,
        reserved=product.reserved,
        sold=product.sold,
        available=product.available,
        orders=order_counts[0],
        pending_orders=order_counts[1],
        distinct_references=order_counts[2],
        intents={IntentOutcome(outcome): count for outcome, count in by_outcome},
        processed_comments=comment_counts[0],
        comments=comment_counts[1],
    )


async def test_a_thousand_concurrent_comments_never_oversell(
    postgres_backend: PostgresBackend, worker_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    comments = [
        queued_comment("je prends Z99", external_comment_id=f"c-{n}", author=f"buyer-{n}")
        for n in range(DEMAND)
    ]
    process, queue, deliveries, session_id = await _setup(
        postgres_backend, comments, stock_total=STOCK
    )

    with PoolProbe(worker_engine) as probe:
        handled = await _race(process, deliveries)

    stored = await _stored(migrator_engine, session_id)
    assert {result.handling for result in handled} == {Handling.PROCESSED}
    outcomes = [
        result.reservation.intent.outcome for result in handled if result.reservation is not None
    ]
    assert outcomes.count(IntentOutcome.RESERVED) == STOCK
    assert outcomes.count(IntentOutcome.OUT_OF_STOCK) == DEMAND - STOCK
    # The invariants of the stock: nothing oversold, no negative counter.
    assert (stored.stock_total, stored.reserved, stored.sold, stored.available) == (
        STOCK,
        STOCK,
        0,
        0,
    )
    assert stored.orders == stored.pending_orders == stored.distinct_references == STOCK
    assert stored.intents == {
        IntentOutcome.RESERVED: STOCK,
        IntentOutcome.OUT_OF_STOCK: DEMAND - STOCK,
    }
    assert stored.processed_comments == stored.comments == DEMAND
    assert queue.pending == []
    assert len(queue.acknowledged) == DEMAND
    # The pool was the bottleneck, and every connection went back to it.
    assert probe.peak == WORKER_POOL.size
    assert probe.checked_out == 0


async def test_twenty_parallel_deliveries_of_one_comment_give_one_effect(
    postgres_backend: PostgresBackend, worker_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    comment = queued_comment("je prends Z99", external_comment_id="c-1", author="buyer-1")
    process, queue, deliveries, session_id = await _setup(
        postgres_backend, [comment] * 20, stock_total=10
    )

    with PoolProbe(worker_engine) as probe:
        handled = await _race(process, deliveries)

    stored = await _stored(migrator_engine, session_id)
    handlings = [result.handling for result in handled]
    assert handlings.count(Handling.PROCESSED) == 1
    assert set(handlings) <= {Handling.PROCESSED, Handling.DUPLICATE, Handling.IN_FLIGHT}
    assert (stored.reserved, stored.available, stored.orders) == (1, 9, 1)
    assert stored.intents == {IntentOutcome.RESERVED: 1}
    assert stored.processed_comments == stored.comments == 1
    assert queue.pending == []
    assert len(queue.acknowledged) == 20
    # The claims overlapped: several connections were busy with them at once.
    assert probe.peak > 1
