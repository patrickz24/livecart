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


async def test_comments_are_delivered_once_in_order(queue: FakeCommentQueue) -> None:
    first = await queue.receive("worker-1", max_messages=2, block_seconds=2)
    rest = await queue.receive("worker-2", max_messages=10, block_seconds=2)

    assert [d.comment for d in first] == [FIRST, SECOND]
    assert [d.comment for d in rest] == [THIRD]
    assert [d.delivery_count for d in first + rest] == [1, 1, 1]
    assert [d.delivery_id for d in first + rest] == ["1-0", "2-0", "3-0"]
    # A pending comment is not handed out again without a reclaim.
    assert await queue.receive("worker-3", max_messages=10, block_seconds=2) == []


async def test_the_backlog_counts_new_and_pending_comments(queue: FakeCommentQueue) -> None:
    assert await queue.backlog() == 3

    deliveries = await queue.receive("worker-1", max_messages=2, block_seconds=0)
    assert await queue.backlog() == 3

    await queue.ack(deliveries[0])
    assert await queue.backlog() == 2
    assert queue.pending == [deliveries[1]]


async def test_acknowledging_twice_does_nothing(queue: FakeCommentQueue) -> None:
    [delivery] = await queue.receive("worker-1", max_messages=1, block_seconds=0)

    await queue.ack(delivery)
    await queue.ack(delivery)

    assert queue.acknowledged == ["1-0"]
    assert await queue.backlog() == 2


async def test_an_empty_queue_returns_at_once() -> None:
    queue = FakeCommentQueue(FakeClock())

    assert await queue.receive("worker-1", max_messages=5, block_seconds=2) == []
    assert await queue.backlog() == 0


async def test_reclaim_hands_idle_comments_again_with_one_more_delivery(
    queue: FakeCommentQueue, clock: FakeClock
) -> None:
    first, second = await queue.receive("worker-1", max_messages=2, block_seconds=0)
    clock.advance(60)
    await queue.ack(second)

    assert await queue.reclaim("worker-2", min_idle_seconds=120, max_messages=10) == []

    clock.advance(60)
    [again] = await queue.reclaim("worker-2", min_idle_seconds=120, max_messages=10)

    assert again.delivery_id == first.delivery_id
    assert again.comment == FIRST
    assert again.delivery_count == 2
    # Reclaimed means delivered just now: not idle any more.
    assert await queue.reclaim("worker-3", min_idle_seconds=120, max_messages=10) == []


async def test_reclaim_takes_the_oldest_first_up_to_the_limit(
    queue: FakeCommentQueue, clock: FakeClock
) -> None:
    await queue.receive("worker-1", max_messages=3, block_seconds=0)
    clock.advance(200)

    reclaimed = await queue.reclaim("worker-2", min_idle_seconds=120, max_messages=2)

    assert [d.comment for d in reclaimed] == [FIRST, SECOND]
    assert [d.delivery_count for d in queue.pending] == [2, 2, 1]


async def test_a_dead_letter_settles_the_delivery(queue: FakeCommentQueue) -> None:
    first, second = await queue.receive("worker-1", max_messages=2, block_seconds=0)

    await queue.dead_letter(first)
    await queue.ack(first)
    await queue.ack(second)
    await queue.dead_letter(second)

    assert queue.dead_letters == [first]
    assert queue.acknowledged == [second.delivery_id]
    assert await queue.backlog() == 1


async def test_a_scripted_failure_enqueues_nothing() -> None:
    queue = FakeCommentQueue(FakeClock())
    queue.fail_next()

    with pytest.raises(ServiceUnavailable, match="scripted failure"):
        await queue.enqueue([FIRST])

    assert await queue.backlog() == 0
    await queue.enqueue([FIRST])
    assert await queue.backlog() == 1


async def test_nonsensical_arguments_are_refused(queue: FakeCommentQueue) -> None:
    with pytest.raises(ValueError, match="max_messages"):
        await queue.receive("worker-1", max_messages=0, block_seconds=0)
    with pytest.raises(ValueError, match="block_seconds"):
        await queue.receive("worker-1", max_messages=1, block_seconds=-1)
    with pytest.raises(ValueError, match="max_messages"):
        await queue.reclaim("worker-1", min_idle_seconds=0, max_messages=0)
    with pytest.raises(ValueError, match="min_idle_seconds"):
        await queue.reclaim("worker-1", min_idle_seconds=-1, max_messages=1)
    with pytest.raises(ValueError, match="count"):
        queue.fail_next(0)
