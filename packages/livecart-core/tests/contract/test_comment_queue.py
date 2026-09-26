"""``CommentQueue`` on every backend: the in-memory fake, then Redis Streams.

Idle time needs time to pass, which the fake's clock and Redis measure
differently: tests/unit/fakes and tests/integration prove reclaiming after a
real idle time apart, with what only Redis shows (its pending entries list,
the dead-letter stream, the trimmed history).
"""

import pytest

from livecart_core.application.dto import QueuedComment, QueueDelivery
from livecart_core.application.ports import CommentQueue
from livecart_core.infrastructure.comment_queue import parse_stream_id
from livecart_core.testing.builders import queued_comment

FIRST, SECOND, THIRD, FOURTH = (queued_comment(external_comment_id=f"c-{n}") for n in range(1, 5))
# Longer than any scenario: a comment delivered during one is never idle for this long.
MINUTES = 600


def _comments(deliveries: list[QueueDelivery]) -> list[QueuedComment]:
    return [delivery.comment for delivery in deliveries]


async def _filled(queue: CommentQueue, *comments: QueuedComment) -> CommentQueue:
    await queue.enqueue(comments or (FIRST, SECOND, THIRD))
    return queue


async def test_comments_are_delivered_once_in_order(comment_queue: CommentQueue) -> None:
    queue = await _filled(comment_queue)

    first = await queue.receive("worker-1", max_messages=2, block_seconds=0)
    rest = await queue.receive("worker-2", max_messages=10, block_seconds=0)

    assert _comments(first) == [FIRST, SECOND]
    assert _comments(rest) == [THIRD]
    assert [delivery.delivery_count for delivery in first + rest] == [1, 1, 1]
    ids = [parse_stream_id(delivery.delivery_id) for delivery in first + rest]
    assert ids == sorted(set(ids))
    # A pending comment is not handed out again without a reclaim.
    assert await queue.receive("worker-3", max_messages=10, block_seconds=0) == []


async def test_a_batch_keeps_its_order_and_every_field(comment_queue: CommentQueue) -> None:
    batch = [
        queued_comment(f"je prends A12 x{n % 9 + 1}", external_comment_id=f"b-{n}")
        for n in range(200)
    ]
    await comment_queue.enqueue(batch)

    received = await comment_queue.receive("worker-1", max_messages=200, block_seconds=0)

    assert _comments(received) == batch


async def test_the_backlog_counts_new_and_pending_comments(comment_queue: CommentQueue) -> None:
    queue = await _filled(comment_queue)
    assert await queue.backlog() == 3

    deliveries = await queue.receive("worker-1", max_messages=2, block_seconds=0)
    assert await queue.backlog() == 3

    await queue.ack(deliveries[0])
    assert await queue.backlog() == 2
    await queue.ack(deliveries[1])
    [last] = await queue.receive("worker-1", max_messages=5, block_seconds=0)
    await queue.ack(last)
    assert await queue.backlog() == 0


async def test_acknowledging_twice_does_nothing(comment_queue: CommentQueue) -> None:
    queue = await _filled(comment_queue)
    [delivery] = await queue.receive("worker-1", max_messages=1, block_seconds=0)

    await queue.ack(delivery)
    await queue.ack(delivery)

    assert await queue.backlog() == 2


async def test_an_empty_queue_returns_nothing(comment_queue: CommentQueue) -> None:
    assert await comment_queue.backlog() == 0
    assert await comment_queue.receive("worker-1", max_messages=5, block_seconds=0) == []
    assert await comment_queue.receive("worker-1", max_messages=5, block_seconds=0.05) == []
    assert await comment_queue.reclaim("worker-1", min_idle_seconds=0, max_messages=5) == []
    await comment_queue.enqueue([])
    assert await comment_queue.backlog() == 0


async def test_reclaim_hands_pending_comments_again_with_one_more_delivery(
    comment_queue: CommentQueue,
) -> None:
    queue = await _filled(comment_queue)
    first, second = await queue.receive("worker-1", max_messages=2, block_seconds=0)
    await queue.ack(second)

    assert await queue.reclaim("worker-2", min_idle_seconds=MINUTES, max_messages=10) == []
    [again] = await queue.reclaim("worker-2", min_idle_seconds=0, max_messages=10)

    assert again.delivery_id == first.delivery_id
    assert again.comment == FIRST
    assert again.delivery_count == 2
    # Reclaimed means delivered just now: not idle any more.
    assert await queue.reclaim("worker-3", min_idle_seconds=MINUTES, max_messages=10) == []
    await queue.ack(again)
    assert await queue.reclaim("worker-3", min_idle_seconds=0, max_messages=10) == []
    assert await queue.backlog() == 1


async def test_reclaim_takes_the_oldest_first_up_to_the_limit(
    comment_queue: CommentQueue,
) -> None:
    queue = await _filled(comment_queue)
    await queue.receive("worker-1", max_messages=3, block_seconds=0)

    reclaimed = await queue.reclaim("worker-2", min_idle_seconds=0, max_messages=2)
    everything = await queue.reclaim("worker-3", min_idle_seconds=0, max_messages=10)

    assert _comments(reclaimed) == [FIRST, SECOND]
    assert [delivery.delivery_count for delivery in reclaimed] == [2, 2]
    assert _comments(everything) == [FIRST, SECOND, THIRD]
    assert [delivery.delivery_count for delivery in everything] == [3, 3, 2]
    # A reclaim never hands out what nobody received.
    assert await queue.receive("worker-1", max_messages=10, block_seconds=0) == []


async def test_a_dead_letter_settles_the_delivery_once(comment_queue: CommentQueue) -> None:
    queue = await _filled(comment_queue)
    first, second = await queue.receive("worker-1", max_messages=2, block_seconds=0)

    await queue.dead_letter(first)
    await queue.ack(first)
    await queue.ack(second)
    await queue.dead_letter(second)

    assert await queue.backlog() == 1
    assert await queue.reclaim("worker-2", min_idle_seconds=0, max_messages=10) == []
    assert _comments(await queue.receive("worker-2", max_messages=10, block_seconds=0)) == [THIRD]


async def test_trim_never_drops_a_new_or_pending_comment(comment_queue: CommentQueue) -> None:
    queue = await _filled(comment_queue, FIRST, SECOND, THIRD, FOURTH)
    first, second = await queue.receive("worker-1", max_messages=2, block_seconds=0)
    await queue.ack(first)

    removed = await queue.trim()

    assert removed >= 0
    assert await queue.backlog() == 3
    [pending] = await queue.reclaim("worker-2", min_idle_seconds=0, max_messages=10)
    assert pending.delivery_id == second.delivery_id
    assert _comments(await queue.receive("worker-2", max_messages=10, block_seconds=0)) == [
        THIRD,
        FOURTH,
    ]
    assert await queue.trim() >= 0
    assert await queue.backlog() == 3


async def test_nonsensical_arguments_are_refused(comment_queue: CommentQueue) -> None:
    with pytest.raises(ValueError, match="max_messages"):
        await comment_queue.receive("worker-1", max_messages=0, block_seconds=0)
    with pytest.raises(ValueError, match="block_seconds"):
        await comment_queue.receive("worker-1", max_messages=1, block_seconds=-1)
    with pytest.raises(ValueError, match="max_messages"):
        await comment_queue.reclaim("worker-1", min_idle_seconds=0, max_messages=0)
    with pytest.raises(ValueError, match="min_idle_seconds"):
        await comment_queue.reclaim("worker-1", min_idle_seconds=-1, max_messages=1)
