"""The comment webhook on a real Redis: a signed batch becomes stream entries a worker reads.

The rate limiter is Redis too, so the platform's counter is the one every
API process shares; the repositories are the fakes, which the path never uses.
"""

import pytest
from redis.asyncio import Redis

from livecart_api.testing import (
    SIMULATOR_WEBHOOK,
    current_time_clock,
    make_app,
    make_settings,
    open_client,
    signed_webhook,
)
from livecart_core.application.dto import CommentBatch
from livecart_core.infrastructure.comment_queue import RedisCommentQueue
from livecart_core.infrastructure.ratelimit import RedisRateLimiter
from livecart_core.testing.builders import incoming_comment
from livecart_core.testing.fakes import InMemoryDatabase

pytestmark = pytest.mark.integration


async def test_a_signed_batch_is_queued_in_the_stream_for_the_workers(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    clock = current_time_clock()
    queue = RedisCommentQueue(redis_client, prefix=redis_key_prefix)
    app = make_app(
        database=InMemoryDatabase(),
        clock=clock,
        rate_limiter=RedisRateLimiter(redis_client, prefix=redis_key_prefix),
        comment_queue=queue,
    )
    batch = CommentBatch(
        comments=[incoming_comment(external_comment_id=f"c-{n}") for n in range(3)]
    )
    headers, body = signed_webhook(batch, clock)

    async with open_client(app) as client:
        accepted = await client.post(SIMULATOR_WEBHOOK, content=body, headers=headers)
        refused = await client.post(SIMULATOR_WEBHOOK, content=body)

    assert (accepted.status_code, accepted.json()) == (202, {"accepted": 3})
    assert refused.status_code == 401
    assert await redis_client.xlen(queue.stream_key) == 3
    deliveries = await queue.receive("worker-1", max_messages=10, block_seconds=0)
    assert [delivery.comment.comment for delivery in deliveries] == list(batch.comments)
    request_id = accepted.headers["X-Request-ID"]
    assert {delivery.comment.request_id for delivery in deliveries} == {request_id}
    assert len({delivery.comment.correlation_id for delivery in deliveries}) == 3
    # Only the signed call counted against the platform's limit.
    assert await redis_client.get(f"{redis_key_prefix}:rl:comment_webhook:simulator") == b"1"
    # The unsigned one counted against its address, in the counter every API shares.
    failures_key = f"{redis_key_prefix}:rl:comment_webhook_signature_failures:127.0.0.1"
    assert await redis_client.get(failures_key) == b"1"


async def test_unsigned_calls_past_the_limit_of_their_address_get_429(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    clock = current_time_clock()
    app = make_app(
        database=InMemoryDatabase(),
        clock=clock,
        rate_limiter=RedisRateLimiter(redis_client, prefix=redis_key_prefix),
        comment_queue=RedisCommentQueue(redis_client, prefix=redis_key_prefix),
        settings=make_settings(rate_limit_comment_webhook_signature_failures_per_minute=3),
    )
    _, body = signed_webhook(CommentBatch(comments=[incoming_comment()]), clock)

    async with open_client(app) as client:
        answers = [
            (await client.post(SIMULATOR_WEBHOOK, content=body)).status_code for _ in "abcde"
        ]

    assert answers == [401, 401, 401, 429, 429]
    failures_key = f"{redis_key_prefix}:rl:comment_webhook_signature_failures:127.0.0.1"
    assert await redis_client.get(failures_key) == b"3"
    assert 0 < await redis_client.ttl(failures_key) <= 60
