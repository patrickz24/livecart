"""The comment consumer over the in-memory queue: its window, handlers, deadline and retries."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest
import structlog

from livecart_core.application.dto import InboxComment, Platform, QueueDelivery
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.application.process_comment import HandledComment, Handling
from livecart_core.domain.state_machines import CommentStatus
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FakeClock, FakeCommentQueue
from livecart_core.testing.polling import until
from livecart_worker.consumer import (
    MAX_HOST_NAME_LENGTH,
    CommentConsumer,
    default_consumer_name,
    full_jitter,
)

DEADLINE_S = 5.0
BLOCK_S = 0.01


class BlockingQueue(FakeCommentQueue):
    """The in-memory queue, but a read that finds nothing waits, as XREADGROUP BLOCK does."""

    def __init__(self) -> None:
        super().__init__(FakeClock())
        self.reads: list[int] = []
        self.unavailable = 0

    async def receive(
        self, consumer: str, *, max_messages: int, block_seconds: float
    ) -> list[QueueDelivery]:
        self.reads.append(max_messages)
        if self.unavailable:
            self.unavailable -= 1
            raise ServiceUnavailable("comment queue unavailable (receive): scripted")
        deliveries = await super().receive(
            consumer, max_messages=max_messages, block_seconds=block_seconds
        )
        if not deliveries:
            await asyncio.sleep(block_seconds)
        return deliveries


def _handled(delivery: QueueDelivery) -> HandledComment:
    comment = delivery.comment.comment
    return HandledComment(
        Handling.PROCESSED,
        InboxComment(
            id=UUID(int=1),
            platform=Platform.SIMULATOR,
            external_comment_id=comment.external_comment_id,
            author_platform_user_id=comment.author_platform_user_id,
            text=comment.text,
            correlation_id=delivery.comment.correlation_id,
            status=CommentStatus.RECEIVED,
            created_at=datetime(2026, 9, 26, tzinfo=UTC),
        ),
    )


class Handler:
    """Acknowledges each delivery as ProcessComment would, after ``behave`` lets it."""

    def __init__(
        self,
        queue: FakeCommentQueue,
        behave: Callable[[QueueDelivery, int], Awaitable[None]] | None = None,
    ) -> None:
        self._queue = queue
        self._behave = behave
        self.calls: list[tuple[str, str]] = []
        self.deadlines: list[float] = []
        self.correlation_ids: list[object] = []
        self.running = 0
        self.most_running = 0

    async def __call__(
        self, delivery: QueueDelivery, *, consumer: str, deadline: float
    ) -> HandledComment:
        self.calls.append((delivery.delivery_id, consumer))
        self.deadlines.append(deadline)
        self.correlation_ids.append(structlog.contextvars.get_contextvars().get("correlation_id"))
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            if self._behave is not None:
                attempt = sum(1 for call in self.calls if call[0] == delivery.delivery_id)
                await self._behave(delivery, attempt)
            await self._queue.ack(delivery)
            return _handled(delivery)
        finally:
            self.running -= 1


def _consumer(
    queue: FakeCommentQueue,
    handler: Handler,
    *,
    batch: int = 32,
    concurrency: int = 16,
    deadline_s: float = DEADLINE_S,
    delays: list[float] | None = None,
) -> CommentConsumer:
    async def no_sleep(seconds: float) -> None:
        if delays is not None:
            delays.append(seconds)

    return CommentConsumer(
        queue,
        handler,
        name="worker-1",
        batch=batch,
        concurrency=concurrency,
        deadline_s=deadline_s,
        block_s=BLOCK_S,
        jitter=lambda cap: cap,
        sleep=no_sleep,
    )


async def _running(consumer: CommentConsumer) -> tuple[asyncio.Event, asyncio.Task[None]]:
    stop = asyncio.Event()
    return stop, asyncio.create_task(consumer.run(stop))


async def _stop(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_every_delivery_is_handled_in_order_by_this_consumer() -> None:
    queue = BlockingQueue()
    handler = Handler(queue)
    await queue.enqueue([queued_comment(external_comment_id=f"c-{n}") for n in range(5)])
    stop, task = await _running(_consumer(queue, handler, concurrency=1))

    await until(lambda: len(queue.acknowledged) == 5)
    await _stop(stop, task)

    assert handler.calls == [(f"{n}-0", "worker-1") for n in range(1, 6)]
    assert queue.pending == []


async def test_each_attempt_is_told_the_one_deadline_of_its_delivery() -> None:
    queue = BlockingQueue()

    async def fail_once(delivery: QueueDelivery, attempt: int) -> None:
        if attempt == 1:
            raise ServiceUnavailable("scripted")

    handler = Handler(queue, fail_once)
    await queue.enqueue([queued_comment()])
    started = asyncio.get_running_loop().time()
    stop, task = await _running(_consumer(queue, handler))

    await until(lambda: len(queue.acknowledged) == 1)
    await _stop(stop, task)

    first, retry = handler.deadlines
    # The retry has what is left of the first attempt's deadline, not a new one.
    assert first == retry
    assert started + DEADLINE_S <= first <= asyncio.get_running_loop().time() + DEADLINE_S


async def test_no_more_handlers_run_at_once_than_the_concurrency() -> None:
    queue = BlockingQueue()
    gate = asyncio.Event()

    async def wait_for_gate(delivery: QueueDelivery, attempt: int) -> None:
        await gate.wait()

    handler = Handler(queue, wait_for_gate)
    await queue.enqueue([queued_comment(external_comment_id=f"c-{n}") for n in range(10)])
    stop, task = await _running(_consumer(queue, handler, batch=10, concurrency=3))

    await until(lambda: handler.running == 3)
    await asyncio.sleep(0.05)
    assert handler.running == 3
    gate.set()
    await until(lambda: len(queue.acknowledged) == 10)
    await _stop(stop, task)

    assert handler.most_running == 3


async def test_the_consumer_holds_no_more_deliveries_than_its_batch() -> None:
    queue = BlockingQueue()
    gate = asyncio.Event()

    async def wait_for_gate(delivery: QueueDelivery, attempt: int) -> None:
        await gate.wait()

    handler = Handler(queue, wait_for_gate)
    await queue.enqueue([queued_comment(external_comment_id=f"c-{n}") for n in range(10)])
    consumer = _consumer(queue, handler, batch=4, concurrency=2)
    stop, task = await _running(consumer)

    await until(lambda: consumer.in_flight == 4)
    await asyncio.sleep(0.05)
    # Four read and pending, two of them running; six left unread for other consumers.
    assert (consumer.in_flight, consumer.room, handler.running) == (4, 0, 2)
    assert len(queue.pending) == 4
    assert await queue.backlog() == 10
    gate.set()
    await until(lambda: len(queue.acknowledged) == 10)
    await _stop(stop, task)

    # Every read asked for the room left, never more.
    assert max(queue.reads) == 4
    assert all(read >= 1 for read in queue.reads)


async def test_a_delivery_past_its_deadline_is_left_pending_and_the_loop_goes_on() -> None:
    queue = BlockingQueue()

    async def first_hangs(delivery: QueueDelivery, attempt: int) -> None:
        if delivery.delivery_id == "1-0":
            await asyncio.Event().wait()

    handler = Handler(queue, first_hangs)
    await queue.enqueue([queued_comment(external_comment_id=f"c-{n}") for n in range(2)])
    with structlog.testing.capture_logs() as logs:
        stop, task = await _running(_consumer(queue, handler, deadline_s=0.05))
        await until(lambda: queue.acknowledged == ["2-0"])
        await until(lambda: any(log["event"] == "comment_deadline_exceeded" for log in logs))
        await _stop(stop, task)

    assert [delivery.delivery_id for delivery in queue.pending] == ["1-0"]
    [exceeded] = [log for log in logs if log["event"] == "comment_deadline_exceeded"]
    assert (exceeded["delivery_id"], exceeded["delivery_count"]) == ("1-0", 1)


async def test_a_transient_failure_is_retried_twice_after_growing_jittered_delays() -> None:
    queue = BlockingQueue()

    async def fails_twice(delivery: QueueDelivery, attempt: int) -> None:
        if attempt <= 2:
            raise ServiceUnavailable("database unavailable: scripted")

    handler = Handler(queue, fails_twice)
    delays: list[float] = []
    await queue.enqueue([queued_comment()])
    stop, task = await _running(_consumer(queue, handler, delays=delays))

    await until(lambda: queue.acknowledged == ["1-0"])
    await _stop(stop, task)

    assert len(handler.calls) == 3
    # Drawn from 0 to 0.1 s, then from 0 to 0.2 s (the jitter here gives its cap).
    assert delays == [0.1, 0.2]


async def test_a_third_transient_failure_leaves_the_delivery_for_the_reclaim() -> None:
    queue = BlockingQueue()

    async def always_fails(delivery: QueueDelivery, attempt: int) -> None:
        raise ServiceUnavailable("database unavailable: scripted")

    handler = Handler(queue, always_fails)
    await queue.enqueue([queued_comment()])
    with structlog.testing.capture_logs() as logs:
        stop, task = await _running(_consumer(queue, handler))
        await until(lambda: any(log["event"] == "comment_left_for_reclaim" for log in logs))
        await _stop(stop, task)

    assert len(handler.calls) == 3
    assert [delivery.delivery_id for delivery in queue.pending] == ["1-0"]
    assert queue.acknowledged == []


async def test_an_unexpected_error_is_logged_and_never_stops_the_loop() -> None:
    queue = BlockingQueue()

    async def first_breaks(delivery: QueueDelivery, attempt: int) -> None:
        if delivery.delivery_id == "1-0":
            raise RuntimeError("a bug")

    handler = Handler(queue, first_breaks)
    await queue.enqueue([queued_comment(external_comment_id=f"c-{n}") for n in range(2)])
    with structlog.testing.capture_logs() as logs:
        stop, task = await _running(_consumer(queue, handler))
        await until(lambda: queue.acknowledged == ["2-0"])
        await _stop(stop, task)

    assert [log["delivery_id"] for log in logs if log["event"] == "comment_failed"] == ["1-0"]
    handled = [log for log in logs if log["event"] == "comment_handled"]
    assert [(log["delivery_id"], log["handling"]) for log in handled] == [("2-0", "processed")]
    assert [delivery.delivery_id for delivery in queue.pending] == ["1-0"]


async def test_every_log_line_of_a_handler_carries_the_comment_ids() -> None:
    queue = BlockingQueue()
    handler = Handler(queue)
    comment = queued_comment(external_comment_id="c-1")
    await queue.enqueue([comment])
    stop, task = await _running(_consumer(queue, handler))

    await until(lambda: len(queue.acknowledged) == 1)
    await _stop(stop, task)

    assert handler.correlation_ids == [comment.correlation_id]
    assert structlog.contextvars.get_contextvars() == {}


async def test_an_unavailable_queue_is_read_again_after_a_pause() -> None:
    queue = BlockingQueue()
    queue.unavailable = 2
    handler = Handler(queue)
    await queue.enqueue([queued_comment()])
    with structlog.testing.capture_logs() as logs:
        stop, task = await _running(_consumer(queue, handler))
        await until(lambda: queue.acknowledged == ["1-0"])
        await _stop(stop, task)

    assert [log["event"] for log in logs].count("comment_queue_unavailable") == 2


async def test_stop_lets_the_handlers_in_flight_finish() -> None:
    queue = BlockingQueue()
    gate = asyncio.Event()

    async def wait_for_gate(delivery: QueueDelivery, attempt: int) -> None:
        await gate.wait()

    handler = Handler(queue, wait_for_gate)
    await queue.enqueue([queued_comment()])
    stop, task = await _running(_consumer(queue, handler))
    await until(lambda: handler.running == 1)

    stop.set()
    await asyncio.sleep(0.05)
    assert not task.done()
    gate.set()
    await asyncio.wait_for(task, timeout=2)

    assert queue.acknowledged == ["1-0"]


async def test_the_reclaim_loop_submits_into_the_same_window() -> None:
    queue = BlockingQueue()
    gate = asyncio.Event()

    async def wait_for_gate(delivery: QueueDelivery, attempt: int) -> None:
        await gate.wait()

    handler = Handler(queue, wait_for_gate)
    consumer = _consumer(queue, handler, batch=2)
    await queue.enqueue([queued_comment(external_comment_id=f"c-{n}") for n in range(2)])
    first, second = await queue.receive("worker-1", max_messages=2, block_seconds=0)

    consumer.submit(first)
    consumer.submit(second)
    with pytest.raises(RuntimeError, match="window of 2 deliveries is full"):
        consumer.submit(first)
    assert consumer.room == 0
    gate.set()
    await consumer.drain()

    assert (consumer.in_flight, consumer.room) == (0, 2)
    assert queue.acknowledged == ["1-0", "2-0"]


async def test_the_heartbeat_sees_whether_the_loop_goes_round() -> None:
    now = [100.0]
    queue = BlockingQueue()
    consumer = CommentConsumer(
        queue,
        Handler(queue),
        name="worker-1",
        batch=1,
        concurrency=1,
        deadline_s=DEADLINE_S,
        monotonic=lambda: now[0],
    )
    assert consumer.progressed_within(30)

    now[0] += 31
    assert not consumer.progressed_within(30)
    stop = asyncio.Event()
    stop.set()
    await consumer.run(stop)

    # A stopped loop no longer goes round, and its last round is still recorded.
    assert not consumer.progressed_within(30)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"batch": 0}, "needs a window"),
        ({"concurrency": 0}, "needs a window"),
        ({"deadline_s": 0}, "needs a window"),
        ({"block_s": 0}, "needs a window"),
        ({"reclaim_interval_s": 0}, "needs a window"),
        ({"retries": -1}, "retries cannot be negative"),
    ],
)
def test_a_consumer_needs_its_bounds(overrides: dict[str, float], message: str) -> None:
    queue = BlockingQueue()
    options: dict[str, float] = {"batch": 1, "concurrency": 1, "deadline_s": 1, **overrides}
    with pytest.raises(ValueError, match=message):
        CommentConsumer(
            queue,
            Handler(queue),
            name="w",
            batch=int(options["batch"]),
            concurrency=int(options["concurrency"]),
            deadline_s=options["deadline_s"],
            block_s=options.get("block_s", 1),
            reclaim_interval_s=options.get("reclaim_interval_s", 30),
            retries=int(options.get("retries", 2)),
        )


def test_a_consumer_is_named_after_its_host_and_process() -> None:
    name = default_consumer_name()

    host, _, pid = name.rpartition("-")
    assert pid.isdecimal()
    assert 1 <= len(host) <= MAX_HOST_NAME_LENGTH
    assert re.fullmatch(r".+-\d+", name)


def test_the_jitter_stays_between_zero_and_its_cap() -> None:
    draws = [full_jitter(0.2) for _ in range(200)]

    assert all(0 <= draw <= 0.2 for draw in draws)
    assert len(set(draws)) > 1
