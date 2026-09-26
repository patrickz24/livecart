"""The reclaim loop on the compose Redis and PostgreSQL: the queue's safety net, end to end.

What a crashed consumer left in the group's pending entries list is
reclaimed and processed by a live one; a comment past its last delivery goes
to the dead-letter stream and its inbox row to ``dead_lettered``; and the
trim of a tick drops the settled history only, never an entry pending or
unread. The idle time is shortened to a fraction of a second, which the
settings would refuse, so that the tests need not wait two minutes.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import QueuedComment
from livecart_core.application.process_comment import ProcessComment
from livecart_core.application.reclaim_comments import ReclaimComments
from livecart_core.domain.state_machines import CommentStatus
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.infrastructure.comment_queue import GROUP, RedisCommentQueue
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.events import NullEventPublisher
from livecart_core.infrastructure.notifier import SimulatedBuyerNotifier
from livecart_core.testing.backends import ORDER_TOKENS, PostgresBackend
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, queued_comment
from livecart_core.testing.polling import until
from livecart_worker.consumer import CommentConsumer

pytestmark = pytest.mark.integration

IDLE = timedelta(milliseconds=200)
MAX_DELIVERIES = 3


@dataclass
class Worker:
    queue: RedisCommentQueue
    consumer: CommentConsumer


@pytest.fixture
async def worker(
    redis_client: Redis, redis_key_prefix: str, postgres_backend: PostgresBackend
) -> AsyncIterator[Worker]:
    """A live worker on a stream of this test: its consumer, and its reclaim at a short idle."""
    # Exact trimming, so that the trim test can check its boundary to the entry.
    queue = RedisCommentQueue(redis_client, prefix=redis_key_prefix, approximate_trim=False)
    clock = SystemClock()
    process = ProcessComment(
        inbox=postgres_backend.comments,
        sessions=postgres_backend.live_sessions,
        orders=postgres_backend.orders,
        queue=queue,
        events=NullEventPublisher(),
        notifier=SimulatedBuyerNotifier(
            redis_client, prefix=redis_key_prefix, link_scheme="livecart", production=False
        ),
        access_token=ORDER_TOKENS.token_for,
        clock=clock,
        lease=timedelta(seconds=30),
        reservation_ttl=timedelta(seconds=900),
        max_quantity=DEMO_MAX_QUANTITY,
    )
    consumer = CommentConsumer(
        queue,
        process,
        name="worker-live",
        batch=32,
        concurrency=16,
        deadline_s=30,
        reclaim=ReclaimComments(
            queue, postgres_backend.comments, clock, min_idle=IDLE, max_deliveries=MAX_DELIVERIES
        ),
        reclaim_interval_s=0.1,
    )
    yield Worker(queue, consumer)
    await consumer.drain()


async def _crashed_with(queue: RedisCommentQueue, *comments: QueuedComment) -> None:
    """The comments read by a consumer that then died: pending, never acknowledged."""
    await queue.enqueue(comments)
    await queue.receive("worker-crashed", max_messages=len(comments), block_seconds=0)


async def _pending(client: Redis, queue: RedisCommentQueue) -> list[tuple[str, str, int]]:
    rows: Any = await client.xpending_range(queue.stream_key, GROUP, "-", "+", 100)
    return [
        (row["message_id"].decode(), row["consumer"].decode(), int(row["times_delivered"]))
        for row in rows
    ]


async def _status(engine: AsyncEngine, external_comment_id: str) -> CommentStatus | None:
    async with engine.connect() as connection:
        status = await connection.scalar(
            select(tables.comments.c.status).where(
                tables.comments.c.external_comment_id == external_comment_id
            )
        )
    return None if status is None else CommentStatus(status)


async def test_what_a_crashed_consumer_left_is_reclaimed_and_processed(
    worker: Worker,
    postgres_backend: PostgresBackend,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    session = await postgres_backend.add_live_session()
    await postgres_backend.add_product(session.id, "B07", stock_total=10)
    await _crashed_with(worker.queue, queued_comment("je prends B07"))
    [(_, owner, deliveries)] = await _pending(redis_client, worker.queue)
    assert (owner, deliveries) == ("worker-crashed", 1)

    await asyncio.sleep(IDLE.total_seconds() * 2)
    await worker.consumer.reclaim_once()
    await worker.consumer.drain()

    assert await _status(migrator_engine, "comment-1") is CommentStatus.PROCESSED
    assert await postgres_backend.count("orders") == 1
    assert await _pending(redis_client, worker.queue) == []
    assert await worker.queue.backlog() == 0


async def test_the_running_consumer_takes_back_what_a_crashed_one_left(
    worker: Worker,
    postgres_backend: PostgresBackend,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    # A regression, found by killing a worker of the compose stack: a reclaim
    # loop of its own never found room in the window, which the read loop kept
    # set aside while it blocked on XREADGROUP.
    session = await postgres_backend.add_live_session()
    await postgres_backend.add_product(session.id, "B07", stock_total=10)
    left = [
        queued_comment("je prends B07", external_comment_id=f"c-{n}", author=f"buyer-{n}")
        for n in range(3)
    ]
    await _crashed_with(worker.queue, *left)
    stop = asyncio.Event()

    task = asyncio.create_task(worker.consumer.run(stop))

    async def settled() -> bool:
        return not await _pending(redis_client, worker.queue)

    try:
        await until(settled, within_s=10, every_s=0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    for n in range(3):
        assert await _status(migrator_engine, f"c-{n}") is CommentStatus.PROCESSED
    assert await worker.queue.backlog() == 0


async def test_a_comment_past_its_last_delivery_is_dead_lettered(
    worker: Worker,
    postgres_backend: PostgresBackend,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
) -> None:
    claimed = queued_comment(external_comment_id="c-claimed")
    never_recorded = queued_comment(external_comment_id="c-never")
    # The first crashed after unit 1; the other never reached PostgreSQL.
    await postgres_backend.comments.claim(
        claimed, holder="worker-crashed/1-0", now=SystemClock().now(), lease=timedelta(seconds=1)
    )
    await _crashed_with(worker.queue, claimed, never_recorded)
    for _ in range(MAX_DELIVERIES - 1):
        await worker.queue.reclaim("worker-crashed", min_idle_seconds=0, max_messages=10)
    assert {row[2] for row in await _pending(redis_client, worker.queue)} == {MAX_DELIVERIES}

    await asyncio.sleep(IDLE.total_seconds() * 2)
    await worker.consumer.reclaim_once()

    assert await _status(migrator_engine, "c-claimed") is CommentStatus.DEAD_LETTERED
    assert await _status(migrator_engine, "c-never") is CommentStatus.DEAD_LETTERED
    assert await _pending(redis_client, worker.queue) == []
    dead: Any = await redis_client.xrange(worker.queue.dead_letter_key)
    assert [
        (QueuedComment.model_validate_json(fields[b"envelope"]), int(fields[b"delivery_count"]))
        for _, fields in dead
    ] == [(claimed, MAX_DELIVERIES + 1), (never_recorded, MAX_DELIVERIES + 1)]
    assert worker.consumer.in_flight == 0


async def test_a_tick_trims_the_settled_history_and_never_a_pending_or_unread_entry(
    worker: Worker,
    postgres_backend: PostgresBackend,
    redis_client: Redis,
) -> None:
    session = await postgres_backend.add_live_session()
    await postgres_backend.add_product(session.id, "B07", stock_total=10)
    settled = [
        queued_comment("je prends B07", external_comment_id=f"c-{n}", author=f"buyer-{n}")
        for n in range(3)
    ]
    await worker.queue.enqueue(settled)
    for delivery in await worker.queue.receive("worker-live", max_messages=3, block_seconds=0):
        worker.consumer.submit(delivery)
    await worker.consumer.drain()
    # One more read by the crashed consumer, too recent to be reclaimed, and one unread.
    pending, unread = (
        queued_comment(external_comment_id="c-pending"),
        queued_comment(external_comment_id="c-unread"),
    )
    await _crashed_with(worker.queue, pending)
    await worker.queue.enqueue([unread])

    await worker.consumer.reclaim_once()

    entries: Any = await redis_client.xrange(worker.queue.stream_key)
    kept = [QueuedComment.model_validate_json(fields[b"envelope"]) for _, fields in entries]
    assert kept == [pending, unread]
    assert [row[1] for row in await _pending(redis_client, worker.queue)] == ["worker-crashed"]
    assert await worker.queue.backlog() == 2
