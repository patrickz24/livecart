"""The reclaim loop over the in-memory queue and inbox: redelivered, or dead-lettered."""

import asyncio
from datetime import timedelta

import pytest
import structlog

from livecart_core.application.dto import QueuedComment, QueueDelivery
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.application.process_comment import HandledComment, Handling
from livecart_core.application.reclaim_comments import ReclaimComments, Reclaimed
from livecart_core.domain.state_machines import CommentStatus
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FakeClock, FakeCommentQueue, InMemoryDatabase
from livecart_core.testing.polling import until
from livecart_worker.consumer import CommentConsumer
from livecart_worker.reclaim import reclaim_once, run_reclaim

IDLE = timedelta(seconds=120)


class Handler:
    """Acknowledges each delivery once ``gate`` opens, as ProcessComment would."""

    def __init__(self, queue: FakeCommentQueue, database: InMemoryDatabase) -> None:
        self._queue = queue
        self._database = database
        self.gate = asyncio.Event()
        self.gate.set()
        self.counts: list[int] = []

    async def __call__(self, delivery: QueueDelivery, *, consumer: str) -> HandledComment:
        self.counts.append(delivery.delivery_count)
        await self.gate.wait()
        claim = await self._database.comments.claim(
            delivery.comment, holder=consumer, now=FakeClock().now(), lease=IDLE
        )
        await self._queue.ack(delivery)
        return HandledComment(Handling.PROCESSED, claim.comment)


class World:
    def __init__(self, *, batch: int = 4, max_deliveries: int = 3) -> None:
        self.clock = FakeClock()
        self.queue = FakeCommentQueue(self.clock)
        self.database = InMemoryDatabase()
        self.handler = Handler(self.queue, self.database)
        self.consumer = CommentConsumer(
            self.queue, self.handler, name="worker-2", batch=batch, concurrency=2, deadline_s=5
        )
        self.reclaim = ReclaimComments(
            self.queue,
            self.database.comments,
            self.clock,
            min_idle=IDLE,
            max_deliveries=max_deliveries,
        )

    async def left_by_a_crash(self, *comments: QueuedComment, deliveries: int = 1) -> None:
        await self.queue.enqueue(comments)
        await self.queue.receive("crashed", max_messages=len(comments), block_seconds=0)
        for _ in range(deliveries - 1):
            await self.queue.reclaim("crashed", min_idle_seconds=0, max_messages=len(comments))
        self.clock.advance(IDLE)


def _comments(count: int) -> list[QueuedComment]:
    return [queued_comment(external_comment_id=f"c-{n}") for n in range(count)]


async def test_what_a_crashed_consumer_left_is_handled_by_this_one() -> None:
    world = World()
    await world.left_by_a_crash(*_comments(2))

    await reclaim_once(world.reclaim, world.consumer)
    await world.consumer.drain()

    assert world.handler.counts == [2, 2]
    assert world.queue.pending == []
    assert (world.consumer.in_flight, world.consumer.room) == (0, 4)


async def test_a_reclaim_takes_only_the_room_left_in_the_window() -> None:
    world = World(batch=3)
    world.handler.gate.clear()
    await world.queue.enqueue(_comments(2))
    for delivery in await world.queue.receive("worker-2", max_messages=2, block_seconds=0):
        world.consumer.submit(delivery)
    await world.left_by_a_crash(*_comments(3))

    await reclaim_once(world.reclaim, world.consumer)

    # Two of its own in flight: room for one reclaimed delivery only.
    assert (world.consumer.in_flight, world.consumer.room) == (3, 0)
    assert [delivery.delivery_count for delivery in world.queue.pending].count(2) == 1
    world.handler.gate.set()
    await world.consumer.drain()


async def test_a_delivery_past_the_last_is_dead_lettered_and_logged() -> None:
    world = World()
    comment = queued_comment(external_comment_id="c-dead")
    await world.left_by_a_crash(comment, deliveries=3)

    with structlog.testing.capture_logs() as logs:
        await reclaim_once(world.reclaim, world.consumer)

    [dead] = [log for log in logs if log["event"] == "comment_dead_lettered"]
    assert dead["log_level"] == "error"
    assert (dead["delivery_id"], dead["delivery_count"], dead["inbox"]) == ("1-0", 4, "applied")
    assert dead["correlation_id"] == comment.correlation_id
    [summary] = [log for log in logs if log["event"] == "queue_reclaimed"]
    assert (summary["redelivered"], summary["dead_lettered"]) == (0, 1)
    assert world.handler.counts == []
    [row] = world.database.tables.comments.values()
    assert row.status is CommentStatus.DEAD_LETTERED
    assert world.consumer.room == 4


async def test_a_quiet_tick_logs_nothing() -> None:
    world = World()

    with structlog.testing.capture_logs() as logs:
        await reclaim_once(world.reclaim, world.consumer)

    assert logs == []
    assert world.consumer.room == 4


class _Failing(ReclaimComments):
    def __init__(self, world: World, error: BaseException) -> None:
        super().__init__(
            world.queue, world.database.comments, world.clock, min_idle=IDLE, max_deliveries=3
        )
        self._error = error

    async def __call__(self, consumer: str, *, room: int) -> Reclaimed:
        raise self._error


@pytest.mark.parametrize(
    ("error", "event"),
    [
        (
            ServiceUnavailable("comment queue unavailable (reclaim): scripted"),
            "queue_reclaim_unavailable",
        ),
        (RuntimeError("a bug"), "queue_reclaim_failed"),
    ],
)
async def test_a_failed_tick_is_logged_and_gives_the_room_back(
    error: Exception, event: str
) -> None:
    world = World()

    with structlog.testing.capture_logs() as logs:
        await reclaim_once(_Failing(world, error), world.consumer)

    assert [log["event"] for log in logs] == [event]
    assert world.consumer.room == 4


async def test_a_cancelled_tick_gives_the_room_back() -> None:
    world = World()

    task = asyncio.create_task(
        reclaim_once(_Failing(world, asyncio.CancelledError()), world.consumer)
    )
    with pytest.raises(asyncio.CancelledError):
        await task

    assert world.consumer.room == 4


async def test_the_loop_ticks_at_startup_then_every_interval_until_stopped() -> None:
    world = World()
    await world.left_by_a_crash(*_comments(1))
    stop = asyncio.Event()

    task = asyncio.create_task(run_reclaim(world.reclaim, world.consumer, stop, interval_s=0.01))
    await until(lambda: world.handler.counts == [2])
    await world.left_by_a_crash(queued_comment(external_comment_id="c-later"))
    await until(lambda: len(world.handler.counts) == 2)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    await world.consumer.drain()

    assert world.queue.pending == []
