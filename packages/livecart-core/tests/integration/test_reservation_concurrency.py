"""No oversell under real concurrency: ReserveStock on PostgreSQL, through a worker-sized pool.

A thousand reservations of one product, released together by an
``asyncio.Barrier``, run through the SQL adapter as livecart_app, three times
over. The test asserts invariants only, never who won or in what order: the
outcome is deterministic, the timing is not. What bounds the real concurrency
is the pool, the worker's 20 connections: a probe records how many were
checked out at once, and the test fails if the pool was never saturated,
since a run without contention would prove nothing.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import IntentSource, ReservationResult, ResolvedIntent
from livecart_core.application.reserve_stock import ReserveStock
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.outcomes import IntentOutcome
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import CommentStatus, OrderStatus
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.engine import WORKER_POOL, create_db_engine
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.testing.backends import PostgresBackend
from livecart_core.testing.database import IntegrationSettings
from livecart_core.testing.fakes import FakeClock

pytestmark = pytest.mark.integration

DEMAND = 1000
STOCK = 200
ROUNDS = 3
TTL = timedelta(seconds=900)
INTENT = ResolvedIntent(
    product_code=ProductCode("Z99"),
    variant=Variant(),
    quantity=Quantity(1),
    source=IntentSource.PARSER,
    confidence=Decimal("1.00"),
    evidence=("je prends", "Z99"),
)


class PoolProbe:
    """Counts the connections checked out of an engine's pool: in all, and at most at once."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.checked_out = 0
        self.peak = 0
        self.checkouts = 0

    def __enter__(self) -> "PoolProbe":
        event.listen(self._engine.sync_engine, "checkout", self._on_checkout)
        event.listen(self._engine.sync_engine, "checkin", self._on_checkin)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self._engine.sync_engine, "checkout", self._on_checkout)
        event.remove(self._engine.sync_engine, "checkin", self._on_checkin)

    def _on_checkout(self, *_: Any) -> None:
        self.checked_out += 1
        self.checkouts += 1
        self.peak = max(self.peak, self.checked_out)

    def _on_checkin(self, *_: Any) -> None:
        self.checked_out -= 1


@dataclass(frozen=True)
class Stored:
    """What the database holds for one product and its session after a round."""

    stock_total: int
    reserved: int
    sold: int
    available: int
    orders: int
    pending_orders: int
    distinct_references: int
    intents: dict[IntentOutcome, int]
    processed_comments: int


@pytest.fixture
async def worker_engine(integration_settings: IntegrationSettings) -> AsyncIterator[AsyncEngine]:
    """livecart_app through the worker's pool: 20 connections and no overflow, an exact bound."""
    engine = create_db_engine(
        integration_settings.database_url,
        application_name="livecart-tests-concurrency",
        pool_size=WORKER_POOL.size,
        max_overflow=0,
        # A thousand callers queue for twenty connections; none may give up.
        pool_timeout_s=120,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def postgres_backend(
    clean_database: None, worker_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> PostgresBackend:
    return PostgresBackend(worker_engine, migrator_engine)


async def _race(
    reserve: ReserveStock, session_id: UUID, comment_ids: list[UUID]
) -> list[ReservationResult | None]:
    """Every reservation at once: each task waits at the barrier until all are ready."""
    barrier = asyncio.Barrier(len(comment_ids))

    async def one(comment_id: UUID) -> ReservationResult | None:
        await barrier.wait()
        return await reserve(comment_id, session_id, INTENT)

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(one(comment_id)) for comment_id in comment_ids]
    return [task.result() for task in tasks]


async def _seed_round(
    backend: PostgresBackend, round_no: int, *, stock_total: int, buyers: list[str]
) -> tuple[UUID, UUID, list[UUID]]:
    """A live session, its product, and one received comment per buyer entry."""
    session = await backend.add_live_session(external_live_id=f"live-round-{round_no}")
    product = await backend.add_product(session.id, "Z99", stock_total=stock_total)
    comments = [
        (new_id(), f"round-{round_no}-comment-{n}", buyer) for n, buyer in enumerate(buyers)
    ]
    await backend.add_comments(comments)
    return session.id, product.id, [comment_id for comment_id, _, _ in comments]


async def _stored(engine: AsyncEngine, session_id: UUID, product_id: UUID) -> Stored:
    products, orders, intents, comments = (
        tables.products,
        tables.orders,
        tables.purchase_intents,
        tables.comments,
    )
    async with engine.connect() as connection:
        product = (
            await connection.execute(select(products).where(products.c.id == product_id))
        ).one()
        order_counts = (
            await connection.execute(
                select(
                    func.count(),
                    func.count().filter(orders.c.status == OrderStatus.PENDING_PAYMENT.value),
                    func.count(orders.c.reference.distinct()),
                ).where(orders.c.product_id == product_id)
            )
        ).one()
        by_outcome = (
            await connection.execute(
                select(intents.c.outcome, func.count())
                .where(intents.c.session_id == session_id)
                .group_by(intents.c.outcome)
            )
        ).all()
        processed = await connection.scalar(
            select(func.count()).where(
                comments.c.session_id == session_id,
                comments.c.status == CommentStatus.PROCESSED.value,
            )
        )
    return Stored(
        stock_total=product.stock_total,
        reserved=product.reserved,
        sold=product.sold,
        available=product.available,
        orders=order_counts[0],
        pending_orders=order_counts[1],
        distinct_references=order_counts[2],
        intents={IntentOutcome(outcome): count for outcome, count in by_outcome},
        processed_comments=int(processed or 0),
    )


@pytest.mark.parametrize("round_no", range(1, ROUNDS + 1))
async def test_a_thousand_concurrent_reservations_never_oversell(
    postgres_backend: PostgresBackend,
    worker_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    round_no: int,
) -> None:
    session_id, product_id, comment_ids = await _seed_round(
        postgres_backend, round_no, stock_total=STOCK, buyers=[f"buyer-{n}" for n in range(DEMAND)]
    )
    reserve = ReserveStock(postgres_backend.orders, FakeClock(), reservation_ttl=TTL)

    with PoolProbe(worker_engine) as probe:
        results = await _race(reserve, session_id, comment_ids)

    stored = await _stored(migrator_engine, session_id, product_id)
    successes = sum(
        result is not None and result.intent.outcome is IntentOutcome.RESERVED for result in results
    )
    # Every comment was settled by this unit, none by another delivery.
    assert None not in results
    # The invariants of the stock: nothing oversold, no negative counter.
    assert stored.stock_total == STOCK
    assert min(stored.reserved, stored.sold, stored.available) >= 0
    assert stored.reserved + stored.sold <= stored.stock_total
    assert stored.available == stored.stock_total - stored.reserved - stored.sold
    # As many successes as there was stock, and one pending order per success.
    assert successes == min(DEMAND, STOCK)
    assert stored.orders == stored.pending_orders == stored.distinct_references == successes
    assert stored.reserved == successes * INTENT.quantity.value
    assert stored.intents == {
        IntentOutcome.RESERVED: successes,
        IntentOutcome.OUT_OF_STOCK: DEMAND - successes,
    }
    assert stored.processed_comments == DEMAND
    # The pool was the bottleneck: every connection in use at once, a thousand units through it.
    assert probe.peak == WORKER_POOL.size
    assert probe.checkouts >= DEMAND
    assert probe.checked_out == 0


@pytest.mark.parametrize("round_no", range(1, ROUNDS + 1))
async def test_twenty_concurrent_comments_of_one_buyer_open_one_order(
    postgres_backend: PostgresBackend,
    worker_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    round_no: int,
) -> None:
    session_id, product_id, comment_ids = await _seed_round(
        postgres_backend, round_no, stock_total=10, buyers=["buyer-1"] * 20
    )
    reserve = ReserveStock(postgres_backend.orders, FakeClock(), reservation_ttl=TTL)

    with PoolProbe(worker_engine) as probe:
        results = await _race(reserve, session_id, comment_ids)

    stored = await _stored(migrator_engine, session_id, product_id)
    outcomes = [result.intent.outcome for result in results if result is not None]
    assert len(outcomes) == 20
    assert outcomes.count(IntentOutcome.RESERVED) == 1
    assert outcomes.count(IntentOutcome.ALREADY_RESERVED) == 19
    # The rolled-back reservations left no trace on the stock.
    assert (stored.orders, stored.reserved, stored.available) == (1, 1, 9)
    assert stored.intents == {IntentOutcome.RESERVED: 1, IntentOutcome.ALREADY_RESERVED: 19}
    assert probe.peak == WORKER_POOL.size
