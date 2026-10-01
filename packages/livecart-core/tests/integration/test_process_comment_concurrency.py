"""ProcessComment under real concurrency, on PostgreSQL through the worker's pool of 20.

Every unit of every delivery runs as livecart_app: the lease (unit 1), the
session resolution, the catalog and unit 3. The deliveries start together
behind an ``asyncio.Barrier``, each for a consumer of its own, as deliveries
spread over several workers would. The tests assert invariants only.

A delivery that fails with ``ServiceUnavailable`` is tried again as
``CommentConsumer`` tries it, twice more after a jittered delay: under the
load of the whole suite, a reservation can wait on the product's row for
more than ``LOCK_TIMEOUT_MS`` (2 s) behind a dozen others, which rolls its
unit back as transient, for another attempt. The consumer's retries are
part of how the worker meets a burst; a delivery still failing after them
fails the test, with the error's type.
"""

import asyncio
import random
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import (
    FallbackReceipt,
    FallbackRequest,
    QueuedComment,
    QueueDelivery,
)
from livecart_core.application.ports import (
    IntentFallback,
    NullIntentFallback,
    ServiceUnavailable,
)
from livecart_core.application.process_comment import HandledComment, Handling, ProcessComment
from livecart_core.domain.outcomes import IntentOutcome, LlmOutcome
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
    FakeIntentFallback,
    ok_receipt,
)
from livecart_core.testing.pools import PoolProbe

pytestmark = pytest.mark.integration

DEMAND = 1000
STOCK = 200
LEASE = timedelta(seconds=30)
TTL = timedelta(seconds=900)
# CommentConsumer's TRANSIENT_RETRIES and RETRY_BASE_DELAY_S
# (livecart_worker.consumer): each delay is drawn from 0 to base x 2 ** (retry - 1).
TRANSIENT_RETRIES = 2
RETRY_BASE_DELAY_S = 0.1
_RANDOM = random.SystemRandom()


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
    backend: PostgresBackend,
    comments: list[QueuedComment],
    *,
    stock_total: int,
    fallback: IntentFallback | None = None,
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
        fallback=fallback if fallback is not None else NullIntentFallback(),
        clock=clock,
        lease=LEASE,
        db_budget=timedelta(seconds=5),
        reservation_ttl=TTL,
        max_quantity=DEMO_MAX_QUANTITY,
    )
    return process, queue, deliveries, session.id


async def _deliver(
    process: ProcessComment, delivery: QueueDelivery, *, consumer: str
) -> HandledComment:
    """One delivery, tried again after a transient failure as ``CommentConsumer`` tries it."""
    retry = 0
    while True:
        try:
            return await process(delivery, consumer=consumer)
        except ServiceUnavailable:
            retry += 1
            if retry > TRANSIENT_RETRIES:
                raise
            await asyncio.sleep(_RANDOM.uniform(0, RETRY_BASE_DELAY_S * 2 ** (retry - 1)))


async def _race(process: ProcessComment, deliveries: list[QueueDelivery]) -> list[HandledComment]:
    """Every delivery at once: each task waits at the barrier until all are ready.

    A delivery that still fails after its retries fails the test once every
    other delivery has ended, with the type and message of each failure.
    """
    barrier = asyncio.Barrier(len(deliveries))

    async def one(n: int, delivery: QueueDelivery) -> HandledComment | Exception:
        await barrier.wait()
        try:
            return await _deliver(process, delivery, consumer=f"worker-{n}")
        except Exception as error:
            return error

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(one(n, delivery)) for n, delivery in enumerate(deliveries)]
    results = [task.result() for task in tasks]
    failures = [
        f"{type(result).__name__}: {result}" for result in results if isinstance(result, Exception)
    ]
    assert failures == [], f"{len(failures)} deliveries failed: {failures}"
    return [result for result in results if not isinstance(result, Exception)]


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


class _PoolWatchingFallback(FakeIntentFallback):
    """Answers after 0.2 s, once the other deliveries are done, and notes the pool's use then."""

    def __init__(self, probe: PoolProbe, receipts: list[FallbackReceipt]) -> None:
        super().__init__(receipts, delay_seconds=0.2)
        self._probe = probe
        self.checked_out_at_the_answer: list[int] = []

    async def resolve(self, request: FallbackRequest) -> FallbackReceipt:
        receipt = await super().resolve(request)
        self.checked_out_at_the_answer.append(self._probe.checked_out)
        return receipt


async def test_twenty_parallel_deliveries_of_an_ambiguous_comment_make_one_llm_call(
    postgres_backend: PostgresBackend, worker_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    # "deux": a quantity in words, which the parser leaves to the fallback.
    comment = queued_comment("je prends deux Z99", external_comment_id="c-1", author="buyer-1")
    probe = PoolProbe(worker_engine)
    # One receipt only, and a call slow enough for every claim to overlap it.
    fallback = _PoolWatchingFallback(probe, [ok_receipt("Z99", quantity=2)])
    process, queue, deliveries, session_id = await _setup(
        postgres_backend, [comment] * 20, stock_total=10, fallback=fallback
    )

    with probe:
        handled = await _race(process, deliveries)

    stored = await _stored(migrator_engine, session_id)
    assert len(fallback.requests) == 1
    [processed] = [result for result in handled if result.handling is Handling.PROCESSED]
    assert processed.llm_outcome is LlmOutcome.OK
    assert (stored.reserved, stored.orders) == (2, 1)
    assert stored.intents == {IntentOutcome.RESERVED: 1}
    assert stored.processed_comments == stored.comments == 1
    assert queue.pending == []
    # The delivery that called held no connection while the fallback answered.
    assert fallback.checked_out_at_the_answer == [0]
    assert probe.checked_out == 0
