import asyncio
import itertools
import uuid
from datetime import timedelta

import pytest

from livecart_core.application.dto import IncomingComment
from livecart_core.application.ingest_comments import (
    BACKLOG_TTL,
    EnvelopeTooLarge,
    IngestComments,
    QueueFull,
    new_correlation_id,
)
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.testing.builders import incoming_comment
from livecart_core.testing.fakes import FakeClock, FakeCommentQueue

COMMENTS = [
    incoming_comment("je prends A12 M", external_comment_id="c-1"),
    incoming_comment("SOLD B07", external_comment_id="c-2", author="buyer-2"),
]
ENVELOPE_BOUND = 5_120


class _CountingQueue(FakeCommentQueue):
    """The fake queue, counting its backlog reads, each of which yields to the event loop."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.backlog_reads = 0

    async def backlog(self) -> int:
        self.backlog_reads += 1
        await asyncio.sleep(0)
        return await super().backlog()


def _ingest(
    queue: FakeCommentQueue,
    clock: FakeClock,
    *,
    max_depth: int = 100,
    max_envelope_bytes: int = ENVELOPE_BOUND,
) -> IngestComments:
    counter = itertools.count(1)
    return IngestComments(
        queue,
        clock,
        max_depth=max_depth,
        max_envelope_bytes=max_envelope_bytes,
        correlation_ids=lambda: f"correlation-{next(counter)}",
    )


def _comments(count: int, *, start: int = 0) -> list[IncomingComment]:
    return [incoming_comment(external_comment_id=f"c-{n}") for n in range(start, start + count)]


async def _consume(queue: FakeCommentQueue) -> None:
    """What the workers do: receive everything, then acknowledge it."""
    for delivery in await queue.receive("worker-1", max_messages=1000, block_seconds=0):
        await queue.ack(delivery)


# --- Envelopes -------------------------------------------------------------------------


async def test_each_comment_is_queued_in_order_in_an_envelope_of_its_own() -> None:
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    ingest = _ingest(queue, clock)

    accepted = await ingest(COMMENTS, request_id="request-1")

    deliveries = await queue.receive("worker-1", max_messages=10, block_seconds=0)
    envelopes = [delivery.comment for delivery in deliveries]
    assert accepted == 2
    assert [envelope.comment for envelope in envelopes] == COMMENTS
    assert [envelope.correlation_id for envelope in envelopes] == [
        "correlation-1",
        "correlation-2",
    ]
    assert {envelope.request_id for envelope in envelopes} == {"request-1"}
    assert {envelope.enqueued_at for envelope in envelopes} == {clock.now()}
    assert ingest.comments_received_total == 2


async def test_a_queue_failure_reaches_the_caller_and_counts_nothing() -> None:
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    ingest = _ingest(queue, clock)
    queue.fail_next()

    with pytest.raises(ServiceUnavailable):
        await ingest(COMMENTS, request_id="request-1")

    assert ingest.comments_received_total == 0
    assert await queue.backlog() == 0


def test_correlation_ids_are_random_uuids() -> None:
    first, second = new_correlation_id(), new_correlation_id()

    assert uuid.UUID(first).version == 4
    assert first != second


def test_the_envelopes_are_built_without_queueing_anything() -> None:
    clock = FakeClock()
    queue = FakeCommentQueue(clock)

    envelopes = _ingest(queue, clock).envelopes(COMMENTS, request_id="request-9")

    assert [envelope.correlation_id for envelope in envelopes] == [
        "correlation-1",
        "correlation-2",
    ]
    assert queue.pending == []


async def test_an_envelope_over_the_bound_refuses_the_whole_batch() -> None:
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    small = len(_ingest(queue, clock).envelopes(COMMENTS[:1], request_id="r")[0].model_dump_json())
    ingest = _ingest(queue, clock, max_envelope_bytes=small + 10)
    batch = [COMMENTS[0], incoming_comment("😀" * 1000, external_comment_id="c-9")]

    with pytest.raises(EnvelopeTooLarge) as excinfo:
        await ingest(batch, request_id="request-1")

    assert (excinfo.value.index, excinfo.value.max_size) == (1, small + 10)
    assert await queue.backlog() == 0
    # At the bound exactly, it passes: the bound is inclusive.
    assert await _ingest(queue, clock, max_envelope_bytes=small)(COMMENTS[:1], request_id="r")


def test_the_largest_comment_of_the_format_fits_the_default_bound() -> None:
    clock = FakeClock()
    comment = incoming_comment(
        "😀" * 1000, external_comment_id="c" * 128, author="a" * 128, external_live_id="l" * 128
    )
    ingest = IngestComments(
        FakeCommentQueue(clock), clock, max_depth=1, max_envelope_bytes=ENVELOPE_BOUND
    )

    [envelope] = ingest.envelopes([comment], request_id=uuid.uuid4().hex)

    assert len(envelope.model_dump_json().encode()) < ENVELOPE_BOUND


def test_nonsensical_bounds_are_refused() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError, match="positive"):
        IngestComments(FakeCommentQueue(clock), clock, max_depth=0, max_envelope_bytes=1)
    with pytest.raises(ValueError, match="positive"):
        IngestComments(FakeCommentQueue(clock), clock, max_depth=1, max_envelope_bytes=0)


# --- Backpressure ----------------------------------------------------------------------


async def test_a_batch_is_refused_once_the_backlog_reaches_the_depth() -> None:
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    ingest = _ingest(queue, clock, max_depth=3)

    assert await ingest(_comments(2), request_id="r-1") == 2
    # 2 < 3: accepted, even though it takes the backlog past the depth.
    assert await ingest(_comments(2, start=2), request_id="r-2") == 2
    with pytest.raises(QueueFull) as excinfo:
        await ingest(_comments(1, start=4), request_id="r-3")

    assert (excinfo.value.backlog, excinfo.value.max_depth) == (4, 3)
    assert await queue.backlog() == 4
    assert ingest.comments_received_total == 4
    assert ingest.queue_backpressure_total == 1


async def test_the_webhook_accepts_again_once_the_workers_consumed_and_acknowledged() -> None:
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    ingest = _ingest(queue, clock, max_depth=3)
    await ingest(_comments(3), request_id="r-1")
    with pytest.raises(QueueFull):
        await ingest(_comments(1, start=3), request_id="r-2")

    await _consume(queue)
    # Within the second, this instance still trusts the backlog it read.
    with pytest.raises(QueueFull):
        await ingest(_comments(1, start=3), request_id="r-3")
    clock.advance(BACKLOG_TTL)

    assert await ingest(_comments(1, start=3), request_id="r-4") == 1
    assert ingest.queue_backlog == 1


async def test_pending_comments_count_until_they_are_acknowledged() -> None:
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    ingest = _ingest(queue, clock, max_depth=2)
    await ingest(_comments(2), request_id="r-1")

    await queue.receive("worker-1", max_messages=10, block_seconds=0)
    clock.advance(BACKLOG_TTL)

    with pytest.raises(QueueFull):
        await ingest(_comments(1, start=2), request_id="r-2")


async def test_the_backlog_is_read_once_per_second_plus_what_this_instance_queued() -> None:
    clock = FakeClock()
    queue = _CountingQueue(clock)
    ingest = _ingest(queue, clock, max_depth=10)

    await ingest(_comments(2), request_id="r-1")
    # Another API instance queues 5: this one does not see them within the second.
    other = _ingest(queue, clock, max_depth=10)
    await other(_comments(5, start=10), request_id="r-other")
    clock.advance(timedelta(milliseconds=999))
    await ingest(_comments(2, start=2), request_id="r-2")

    assert queue.backlog_reads == 2  # one per instance
    assert ingest.queue_backlog == 4
    clock.advance(timedelta(milliseconds=1))
    await ingest(_comments(1, start=4), request_id="r-3")
    assert queue.backlog_reads == 3
    assert ingest.queue_backlog == 10


async def test_concurrent_batches_share_one_backlog_read() -> None:
    clock = FakeClock()
    queue = _CountingQueue(clock)
    ingest = _ingest(queue, clock, max_depth=1000)

    accepted = await asyncio.gather(
        *(ingest(_comments(1, start=n), request_id=f"r-{n}") for n in range(10))
    )

    assert accepted == [1] * 10
    assert queue.backlog_reads == 1
    assert ingest.queue_backlog == 10


async def test_a_clock_set_back_reads_the_backlog_again() -> None:
    clock = FakeClock()
    queue = _CountingQueue(clock)
    ingest = _ingest(queue, clock)
    await ingest(_comments(1), request_id="r-1")

    clock.set(clock.now() - timedelta(minutes=5))
    await ingest(_comments(1, start=1), request_id="r-2")

    assert queue.backlog_reads == 2


async def test_an_unreadable_backlog_refuses_the_batch_as_unavailable() -> None:
    clock = FakeClock()

    class _Unreadable(FakeCommentQueue):
        async def backlog(self) -> int:
            raise ServiceUnavailable("backlog unreadable")

    queue = _Unreadable(clock)

    with pytest.raises(ServiceUnavailable):
        await _ingest(queue, clock)(COMMENTS, request_id="r-1")
    assert queue.pending == []
