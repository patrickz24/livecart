"""Backpressure on a real Redis: 503 once the backlog reaches the depth, 202 once it is settled.

The depth is lowered to 50, and the test plays the workers: it receives the
comments from the consumer group and acknowledges them. The backlog is the
group's ``lag + pending``, so the settled history left in the stream, which
the workers trim later, never keeps the webhook refusing.
"""

import httpx2
import pytest
from redis.asyncio import Redis

from livecart_api.routes.webhooks import QUEUE_FULL_RETRY_AFTER_S
from livecart_api.testing import (
    SIMULATOR_WEBHOOK,
    current_time_clock,
    make_app,
    make_settings,
    open_client,
    signed_webhook,
)
from livecart_core.application.dto import CommentBatch
from livecart_core.application.ingest_comments import BACKLOG_TTL
from livecart_core.infrastructure.comment_queue import RedisCommentQueue
from livecart_core.testing.builders import incoming_comment
from livecart_core.testing.fakes import FakeClock, InMemoryDatabase

pytestmark = pytest.mark.integration

DEPTH = 50
BATCH_SIZE = 20


def _batch(number: int) -> CommentBatch:
    return CommentBatch(
        comments=[
            incoming_comment(external_comment_id=f"c-{number}-{n}") for n in range(BATCH_SIZE)
        ]
    )


async def _post(client: httpx2.AsyncClient, number: int, clock: FakeClock) -> httpx2.Response:
    headers, body = signed_webhook(_batch(number), clock)
    return await client.post(SIMULATOR_WEBHOOK, content=body, headers=headers)


async def _settle_everything(queue: RedisCommentQueue) -> int:
    """What the workers do: receive every comment, then acknowledge it; returns how many."""
    settled = 0
    while deliveries := await queue.receive("worker-1", max_messages=32, block_seconds=0):
        for delivery in deliveries:
            await queue.ack(delivery)
        settled += len(deliveries)
    return settled


async def test_a_full_backlog_answers_503_and_a_settled_one_202_again(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    clock = current_time_clock()
    queue = RedisCommentQueue(redis_client, prefix=redis_key_prefix)
    app = make_app(
        database=InMemoryDatabase(),
        clock=clock,
        comment_queue=queue,
        settings=make_settings(queue_max_depth=DEPTH),
    )

    async with open_client(app) as client:
        # 0, 20 and 40 are below 50: the third batch takes the backlog to 60.
        accepted = [(await _post(client, number, clock)).status_code for number in range(3)]
        full = await _post(client, 3, clock)
        assert await queue.backlog() == 60

        # Delivered but not acknowledged still counts: the workers are not done.
        pending = await queue.receive("worker-1", max_messages=DEPTH, block_seconds=0)
        clock.advance(BACKLOG_TTL)
        while_pending = await _post(client, 4, clock)

        for delivery in pending:
            await queue.ack(delivery)
        settled = len(pending) + await _settle_everything(queue)
        clock.advance(BACKLOG_TTL)
        again = await _post(client, 5, clock)

    assert accepted == [202, 202, 202]
    assert (full.status_code, full.headers["Retry-After"]) == (503, str(QUEUE_FULL_RETRY_AFTER_S))
    assert while_pending.status_code == 503
    # More than the depth was processed and acknowledged; the stream keeps its
    # untrimmed history, which the backlog leaves out.
    assert settled == 60 > DEPTH
    assert again.status_code == 202
    assert await redis_client.xlen(queue.stream_key) == 80
    assert await queue.backlog() == BATCH_SIZE
