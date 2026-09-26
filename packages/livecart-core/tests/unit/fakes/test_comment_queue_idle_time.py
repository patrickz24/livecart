"""What only the fake comment queue promises: idle time on its clock, and what it records.

The scenarios it shares with Redis Streams are in tests/contract.
"""

import pytest

from livecart_core.application.ports import ServiceUnavailable
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FakeClock, FakeCommentQueue

FIRST, SECOND, THIRD = (queued_comment(external_comment_id=f"comment-{n}") for n in (1, 2, 3))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def queue(clock: FakeClock) -> FakeCommentQueue:
    queue = FakeCommentQueue(clock)
    await queue.enqueue([FIRST, SECOND, THIRD])
    return queue


async def test_delivery_ids_have_the_shape_of_stream_entry_ids(queue: FakeCommentQueue) -> None:
    deliveries = await queue.receive("worker-1", max_messages=3, block_seconds=2)

    assert [delivery.delivery_id for delivery in deliveries] == ["1-0", "2-0", "3-0"]


async def test_reclaim_waits_for_the_idle_time_on_the_clock(
    queue: FakeCommentQueue, clock: FakeClock
) -> None:
    first, second = await queue.receive("worker-1", max_messages=2, block_seconds=0)
    clock.advance(60)
    await queue.ack(second)

    assert await queue.reclaim("worker-2", min_idle_seconds=120, max_messages=10) == []

    clock.advance(60)
    [again] = await queue.reclaim("worker-2", min_idle_seconds=120, max_messages=10)

    assert (again.delivery_id, again.delivery_count) == (first.delivery_id, 2)
    # Reclaimed means delivered just now: idle again only 120 s later.
    clock.advance(119)
    assert await queue.reclaim("worker-3", min_idle_seconds=120, max_messages=10) == []


async def test_reclaim_takes_only_the_comments_idle_long_enough(
    queue: FakeCommentQueue, clock: FakeClock
) -> None:
    await queue.receive("worker-1", max_messages=1, block_seconds=0)
    clock.advance(200)
    await queue.receive("worker-1", max_messages=2, block_seconds=0)

    reclaimed = await queue.reclaim("worker-2", min_idle_seconds=120, max_messages=10)

    assert [delivery.comment for delivery in reclaimed] == [FIRST]
    assert [delivery.delivery_count for delivery in queue.pending] == [2, 1, 1]


async def test_the_fake_records_acknowledgements_and_dead_letters(
    queue: FakeCommentQueue,
) -> None:
    first, second, third = await queue.receive("worker-1", max_messages=3, block_seconds=0)

    await queue.ack(second)
    await queue.ack(second)
    await queue.dead_letter(first)
    await queue.dead_letter(first)

    assert queue.acknowledged == [second.delivery_id]
    assert queue.dead_letters == [first]
    assert queue.pending == [third]
    assert await queue.trim() == 0


async def test_a_scripted_failure_enqueues_nothing() -> None:
    queue = FakeCommentQueue(FakeClock())
    queue.fail_next()

    with pytest.raises(ServiceUnavailable, match="scripted failure"):
        await queue.enqueue([FIRST])

    assert await queue.backlog() == 0
    await queue.enqueue([FIRST])
    assert await queue.backlog() == 1
    with pytest.raises(ValueError, match="count"):
        queue.fail_next(0)
