"""The consumer's reclaim over the in-memory queue and inbox: redelivered, or dead-lettered."""

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

IDLE = timedelta(seconds=120)


class BlockingQueue(FakeCommentQueue):
    """The in-memory queue, but a read that finds nothing waits, as XREADGROUP BLOCK does."""

    async def receive(
        self, consumer: str, *, max_messages: int, block_seconds: float
    ) -> list[QueueDelivery]:
        deliveries = await super().receive(
            consumer, max_messages=max_messages, block_seconds=block_seconds
        )
        if not deliveries:
            await asyncio.sleep(block_seconds)
        return deliveries


class Handler:
    """Acknowledges each delivery once ``gate`` opens, as ProcessComment would."""

    def __init__(self, queue: FakeCommentQueue, database: InMemoryDatabase) -> None:
        self._queue = queue
        self._database = database
        self.gate = asyncio.Event()
        self.gate.set()
        self.counts: list[int] = []

    async def __call__(
        self, delivery: QueueDelivery, *, consumer: str, deadline: float
    ) -> HandledComment:
        self.counts.append(delivery.delivery_count)
        await self.gate.wait()
        claim = await self._database.comments.claim(
            delivery.comment, holder=consumer, now=FakeClock().now(), lease=IDLE
        )
        await self._queue.ack(delivery)
        return HandledComment(Handling.PROCESSED, claim.comment)


class _Failing(ReclaimComments):
    error: BaseException = ServiceUnavailable("comment queue unavailable (reclaim): scripted")

    async def __call__(self, consumer: str, *, room: int) -> Reclaimed:
        raise self.error


class _Broken(_Failing):
    error = RuntimeError("a bug")


class World:
    def __init__(
        self,
        *,
        batch: int = 4,
        reclaim: type[ReclaimComments] = ReclaimComments,
        interval_s: float = 30,
    ) -> None:
        self.clock = FakeClock()
        self.queue = BlockingQueue(self.clock)
        self.database = InMemoryDatabase()
        self.handler = Handler(self.queue, self.database)
        self.consumer = CommentConsumer(
            self.queue,
            self.handler,
            name="worker-2",
            batch=batch,
            concurrency=2,
            deadline_s=5,
            block_s=0.01,
            reclaim=reclaim(
                self.queue, self.database.comments, self.clock, min_idle=IDLE, max_deliveries=3
            ),
            reclaim_interval_s=interval_s,
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

    await world.consumer.reclaim_once()
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

    await world.consumer.reclaim_once()

    # Two of its own in flight: room for one reclaimed delivery only.
    assert (world.consumer.in_flight, world.consumer.room) == (3, 0)
    assert [delivery.delivery_count for delivery in world.queue.pending].count(2) == 1
    world.handler.gate.set()
    await world.consumer.drain()


async def test_the_running_loop_reclaims_at_startup_and_every_interval() -> None:
    # A regression: a reclaim loop of its own found the window's room set aside
    # by the read loop blocked on XREADGROUP, and never reclaimed anything.
    world = World(interval_s=0.05)
    await world.left_by_a_crash(*_comments(2))
    stop = asyncio.Event()

    task = asyncio.create_task(world.consumer.run(stop))
    await until(lambda: world.handler.counts == [2, 2])
    await world.left_by_a_crash(queued_comment(external_comment_id="c-later"))
    await until(lambda: len(world.handler.counts) == 3)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert world.queue.pending == []


async def test_a_delivery_past_the_last_is_dead_lettered_and_logged() -> None:
    world = World()
    comment = queued_comment(external_comment_id="c-dead")
    await world.left_by_a_crash(comment, deliveries=3)

    with structlog.testing.capture_logs() as logs:
        await world.consumer.reclaim_once()

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


async def test_a_quiet_reclaim_logs_nothing() -> None:
    world = World()

    with structlog.testing.capture_logs() as logs:
        await world.consumer.reclaim_once()

    assert logs == []


@pytest.mark.parametrize(
    ("reclaim", "event"),
    [(_Failing, "queue_reclaim_unavailable"), (_Broken, "queue_reclaim_failed")],
)
async def test_a_failed_reclaim_is_logged_and_never_raised(
    reclaim: type[ReclaimComments], event: str
) -> None:
    world = World(reclaim=reclaim)

    with structlog.testing.capture_logs() as logs:
        await world.consumer.reclaim_once()

    assert [log["event"] for log in logs] == [event]
    assert world.consumer.room == 4


async def test_a_consumer_without_a_reclaim_only_reads() -> None:
    queue = BlockingQueue(FakeClock())
    consumer = CommentConsumer(
        queue, Handler(queue, InMemoryDatabase()), name="w", batch=1, concurrency=1, deadline_s=1
    )

    with pytest.raises(RuntimeError, match="no reclaim"):
        await consumer.reclaim_once()
