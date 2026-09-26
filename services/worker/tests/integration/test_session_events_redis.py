"""The session events end to end: the worker processes a comment, its session's stream holds them.

On the compose PostgreSQL and Redis, through ``serve``: after unit 3 commits,
the intent, the order and the product's stock reach the stream of the
session, in that order, under the comment's correlation id, readable by the
SSE endpoint's own adapter.
"""

import asyncio
from pathlib import Path

import pytest
from redis.asyncio import Redis

from livecart_core.application.dto import SessionEventType, StreamedEvent
from livecart_core.application.ports import START_CURSOR
from livecart_core.infrastructure.comment_queue import RedisCommentQueue
from livecart_core.infrastructure.events import RedisSessionEvents
from livecart_core.settings import Settings
from livecart_core.testing.backends import PostgresBackend
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.database import IntegrationSettings
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY
from livecart_core.testing.polling import until
from livecart_worker.main import serve

pytestmark = pytest.mark.integration


async def test_a_processed_comment_publishes_its_events_after_the_commit(
    integration_settings: IntegrationSettings,
    postgres_backend: PostgresBackend,
    redis_client: Redis,
    redis_key_prefix: str,
    tmp_path: Path,
) -> None:
    session = await postgres_backend.add_live_session()
    product = await postgres_backend.add_product(session.id, "B07", stock_total=10)
    settings = Settings(
        database_url=integration_settings.database_url,
        redis_url=integration_settings.redis_url,
        redis_key_prefix=redis_key_prefix,
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        worker_heartbeat_file=tmp_path / "worker.heartbeat",
        _env_file=None,
    )
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)
    comment = queued_comment("je prends B07 x2", author="buyer-7")
    stop = asyncio.Event()

    worker = asyncio.create_task(serve(settings, stop, consumer_name="worker-events"))
    await RedisCommentQueue(redis_client, prefix=redis_key_prefix).enqueue([comment])
    streamed: list[StreamedEvent] = []

    async def published() -> bool:
        streamed[:] = await events.read(
            session.id, after=START_CURSOR, max_events=10, block_seconds=0
        )
        return len(streamed) == 3

    await until(published, within_s=15, every_s=0.05)
    stop.set()
    await asyncio.wait_for(worker, timeout=35)

    assert [item.event.type for item in streamed] == [
        SessionEventType.INTENT_RECORDED,
        SessionEventType.ORDER_UPDATED,
        SessionEventType.STOCK_UPDATED,
    ]
    assert {item.event.correlation_id for item in streamed} == {comment.correlation_id}
    intent, order, stock = (item.event.data for item in streamed)
    assert intent["outcome"] == "reserved"
    assert intent["order_id"] == order["order_id"]
    assert order["status"] == "pending_payment"
    assert stock == {
        "product_id": str(product.id),
        "stock_total": 10,
        "reserved": 2,
        "sold": 0,
        "available": 8,
    }
    # The buyer's identity never reaches the dashboard's stream.
    assert "buyer-7" not in repr(streamed)
    assert await events.cursor(session.id) == streamed[-1].event_id
    ttl = await redis_client.ttl(events.stream_key(session.id))
    assert 86_000 < ttl <= 86_400
