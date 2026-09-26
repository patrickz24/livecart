"""``RedisCommentQueue`` on a real Redis: what only Redis shows.

The pending entries list and its owners, a reclaim after a real idle time,
the dead-letter stream, the trimmed history and the backlog read from the
group. tests/contract holds the scenarios the fake shares.
"""

import asyncio
import json
from typing import Any

import pytest
from redis.asyncio import Redis

from livecart_core.application.dto import QueuedComment
from livecart_core.infrastructure.comment_queue import GROUP, RedisCommentQueue
from livecart_core.testing.builders import queued_comment

pytestmark = pytest.mark.integration


@pytest.fixture
def queue(redis_client: Redis, redis_key_prefix: str) -> RedisCommentQueue:
    """Trimming exactly, so that the tests can check the boundary to the entry."""
    return RedisCommentQueue(redis_client, prefix=redis_key_prefix, approximate_trim=False)


def _batch(count: int, *, start: int = 0) -> list[QueuedComment]:
    return [queued_comment(external_comment_id=f"c-{n}") for n in range(start, start + count)]


async def _entries(client: Redis, key: str) -> list[tuple[bytes, dict[bytes, bytes]]]:
    """Every entry of a stream, oldest first, as this client reads them (RESP2, bytes)."""
    entries: Any = await client.xrange(key)
    return [(entry_id, dict(fields)) for entry_id, fields in entries]


async def _entry_ids(client: Redis, key: str) -> list[str]:
    return [entry_id.decode() for entry_id, _ in await _entries(client, key)]


async def _pending(client: Redis, queue: RedisCommentQueue) -> list[tuple[str, str, int]]:
    """(id, consumer, times delivered) of each pending entry, oldest first."""
    rows: Any = await client.xpending_range(queue.stream_key, GROUP, "-", "+", 1000)
    return [
        (row["message_id"].decode(), row["consumer"].decode(), int(row["times_delivered"]))
        for row in rows
    ]


async def test_each_comment_is_one_entry_holding_its_envelope(
    queue: RedisCommentQueue, redis_client: Redis, redis_key_prefix: str
) -> None:
    comments = _batch(2)

    await queue.enqueue(comments)

    assert queue.stream_key == f"{redis_key_prefix}:queue:comments"
    entries = await _entries(redis_client, queue.stream_key)
    assert [set(fields) for _, fields in entries] == [{b"envelope"}, {b"envelope"}]
    envelopes = [json.loads(fields[b"envelope"]) for _, fields in entries]
    assert [QueuedComment.model_validate(envelope) for envelope in envelopes] == comments
    assert envelopes[0]["correlation_id"] == "correlation-c-0"
    assert envelopes[0]["request_id"] == "request-c-0"


async def test_nothing_caps_the_length_of_the_stream(
    queue: RedisCommentQueue, redis_client: Redis
) -> None:
    for start in range(0, 1000, 200):
        await queue.enqueue(_batch(200, start=start))

    assert await redis_client.xlen(queue.stream_key) == 1000
    info = await redis_client.xinfo_stream(queue.stream_key)
    assert info["max-deleted-entry-id"] == b"0-0"


async def test_comments_queued_before_any_worker_are_delivered(
    queue: RedisCommentQueue, redis_client: Redis
) -> None:
    await queue.enqueue(_batch(3))
    assert await redis_client.xinfo_groups(queue.stream_key) == []

    received = await queue.receive("worker-1", max_messages=10, block_seconds=0)

    assert [delivery.comment for delivery in received] == _batch(3)
    [group] = await redis_client.xinfo_groups(queue.stream_key)
    assert group["name"] == GROUP.encode()
    await queue.ensure_group()  # a second creation is a no-op


async def test_a_delivery_stays_pending_for_its_consumer_until_acknowledged(
    queue: RedisCommentQueue, redis_client: Redis
) -> None:
    await queue.enqueue(_batch(3))
    first, second = await queue.receive("worker-1", max_messages=2, block_seconds=0)

    assert await _pending(redis_client, queue) == [
        (first.delivery_id, "worker-1", 1),
        (second.delivery_id, "worker-1", 1),
    ]
    await queue.ack(first)
    assert await _pending(redis_client, queue) == [(second.delivery_id, "worker-1", 1)]


async def test_a_comment_idle_past_the_minimum_is_reclaimed_by_another_consumer(
    queue: RedisCommentQueue, redis_client: Redis
) -> None:
    await queue.enqueue(_batch(2))
    stuck, done = await queue.receive("worker-1", max_messages=2, block_seconds=0)
    await queue.ack(done)

    assert await queue.reclaim("worker-2", min_idle_seconds=0.3, max_messages=10) == []
    await asyncio.sleep(0.4)
    [reclaimed] = await queue.reclaim("worker-2", min_idle_seconds=0.3, max_messages=10)

    assert (reclaimed.delivery_id, reclaimed.comment) == (stuck.delivery_id, stuck.comment)
    assert reclaimed.delivery_count == 2
    assert await _pending(redis_client, queue) == [(stuck.delivery_id, "worker-2", 2)]


async def test_a_blocking_receive_hands_a_comment_queued_while_it_waits(
    queue: RedisCommentQueue,
) -> None:
    await queue.ensure_group()
    waiting = asyncio.create_task(queue.receive("worker-1", max_messages=5, block_seconds=2))
    await asyncio.sleep(0.2)

    await queue.enqueue(_batch(1))

    [delivery] = await asyncio.wait_for(waiting, timeout=3)
    assert delivery.comment == _batch(1)[0]


async def test_a_dead_letter_moves_the_entry_to_the_dead_letter_stream_once(
    queue: RedisCommentQueue, redis_client: Redis
) -> None:
    await queue.enqueue(_batch(1))
    [delivery] = await queue.receive("worker-1", max_messages=1, block_seconds=0)
    [again] = await queue.reclaim("worker-2", min_idle_seconds=0, max_messages=1)

    await queue.dead_letter(again)
    await queue.dead_letter(again)

    assert queue.dead_letter_key == f"{queue.stream_key}:dlq"
    [(_, fields)] = await _entries(redis_client, queue.dead_letter_key)
    assert QueuedComment.model_validate_json(fields[b"envelope"]) == delivery.comment
    assert fields[b"delivery_id"].decode() == delivery.delivery_id
    assert fields[b"delivery_count"] == b"2"
    assert await _pending(redis_client, queue) == []


async def test_an_entry_that_is_no_envelope_goes_to_the_dead_letters_at_once(
    queue: RedisCommentQueue, redis_client: Redis
) -> None:
    await queue.ensure_group()
    broken = await redis_client.xadd(queue.stream_key, {"envelope": "{not json"})
    unknown = await redis_client.xadd(queue.stream_key, {"other": "field"})
    await queue.enqueue(_batch(1))

    [delivery] = await queue.receive("worker-1", max_messages=10, block_seconds=0)

    assert delivery.comment == _batch(1)[0]
    dead = await _entries(redis_client, queue.dead_letter_key)
    assert [fields[b"delivery_id"] for _, fields in dead] == [broken, unknown]
    assert [fields[b"envelope"] for _, fields in dead] == [b"{not json", b""]
    assert await _pending(redis_client, queue) == [(delivery.delivery_id, "worker-1", 1)]


async def test_trim_drops_only_what_every_consumer_settled(
    queue: RedisCommentQueue, redis_client: Redis
) -> None:
    await queue.enqueue(_batch(5))
    ids = await _entry_ids(redis_client, queue.stream_key)
    first, second, third = await queue.receive("worker-1", max_messages=3, block_seconds=0)
    await queue.ack(first)
    await queue.ack(third)

    # Below the oldest pending entry (the second), not the last delivered one (the third).
    assert await queue.trim() == 1
    assert await _entry_ids(redis_client, queue.stream_key) == ids[1:]

    await queue.ack(second)
    # Below the last delivered entry: it stays, as do the two nobody has read.
    assert await queue.trim() == 1
    assert await _entry_ids(redis_client, queue.stream_key) == ids[2:]
    assert await queue.trim() == 0
    unread = await queue.receive("worker-2", max_messages=10, block_seconds=0)
    assert [delivery.delivery_id for delivery in unread] == ids[3:]


async def test_an_approximate_trim_drops_whole_nodes_never_a_pending_or_unread_entry(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    queue = RedisCommentQueue(redis_client, prefix=redis_key_prefix)
    await queue.enqueue(_batch(200))
    await queue.enqueue(_batch(100, start=200))
    ids = await _entry_ids(redis_client, queue.stream_key)
    delivered = await queue.receive("worker-1", max_messages=250, block_seconds=0)
    stuck = delivered[150]
    for delivery in delivered:
        if delivery is not stuck:
            await queue.ack(delivery)

    removed = await queue.trim()

    remaining = await _entry_ids(redis_client, queue.stream_key)
    assert 0 < removed <= 150
    assert len(remaining) == len(ids) - removed
    # Whole nodes, from the start: what remains is one run of the stream, from
    # somewhere before the pending entry to the end.
    assert remaining == ids[removed:]
    assert stuck.delivery_id in remaining
    unread = await queue.receive("worker-2", max_messages=100, block_seconds=0)
    assert [delivery.delivery_id for delivery in unread] == ids[250:]
    assert await queue.backlog() == 51


async def test_the_backlog_ignores_the_settled_history(
    queue: RedisCommentQueue, redis_client: Redis
) -> None:
    await queue.enqueue(_batch(10))
    for delivery in await queue.receive("worker-1", max_messages=10, block_seconds=0):
        await queue.ack(delivery)

    assert await queue.backlog() == 0
    assert await redis_client.xlen(queue.stream_key) == 10


async def test_the_backlog_falls_back_to_the_stream_length_when_redis_cannot_tell_the_lag(
    queue: RedisCommentQueue, redis_client: Redis
) -> None:
    await queue.enqueue(_batch(5))
    first, second = await queue.receive("worker-1", max_messages=2, block_seconds=0)
    await queue.ack(first)
    assert await queue.backlog() == 4

    # A group moved to an id in the middle of the stream no longer knows how
    # many entries it read (it could tell for the first one).
    await redis_client.xgroup_setid(queue.stream_key, GROUP, second.delivery_id)
    [group] = await redis_client.xinfo_groups(queue.stream_key)
    assert group["lag"] is None

    assert await queue.backlog() == 5


async def test_trim_without_a_group_drops_nothing(
    queue: RedisCommentQueue, redis_client: Redis
) -> None:
    assert await queue.trim() == 0

    await queue.enqueue(_batch(3))

    assert await queue.trim() == 0
    assert await redis_client.xlen(queue.stream_key) == 3
