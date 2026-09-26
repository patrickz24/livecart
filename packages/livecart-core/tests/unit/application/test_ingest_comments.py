import itertools
import uuid

import pytest

from livecart_core.application.ingest_comments import IngestComments, new_correlation_id
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.testing.builders import incoming_comment
from livecart_core.testing.fakes import FakeClock, FakeCommentQueue

COMMENTS = [
    incoming_comment("je prends A12 M", external_comment_id="c-1"),
    incoming_comment("SOLD B07", external_comment_id="c-2", author="buyer-2"),
]


def _ingest(queue: FakeCommentQueue, clock: FakeClock) -> IngestComments:
    counter = itertools.count(1)
    return IngestComments(queue, clock, correlation_ids=lambda: f"correlation-{next(counter)}")


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
