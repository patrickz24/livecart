"""The worker's comment consumer against the compose PostgreSQL and Redis, through ``serve``.

Comments queued on the real stream are leased, parsed and reserved in
livecart_test as livecart_app, and every delivery is acknowledged: the
group's pending entries list ends empty. A real lock timeout is retried in
the process rather than left to the reclaim.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.domain.state_machines import CommentStatus
from livecart_core.infrastructure.comment_queue import GROUP, RedisCommentQueue
from livecart_core.infrastructure.db import tables
from livecart_core.settings import Settings
from livecart_core.testing.backends import PostgresBackend
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.database import IntegrationSettings
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY
from livecart_core.testing.polling import until
from livecart_worker.main import serve

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(
    integration_settings: IntegrationSettings, redis_key_prefix: str, tmp_path: Path
) -> Settings:
    return Settings(
        database_url=integration_settings.database_url,
        redis_url=integration_settings.redis_url,
        redis_key_prefix=redis_key_prefix,
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        worker_heartbeat_file=tmp_path / "worker.heartbeat",
        _env_file=None,
    )


@pytest.fixture
def queue(redis_client: Redis, redis_key_prefix: str) -> RedisCommentQueue:
    return RedisCommentQueue(redis_client, prefix=redis_key_prefix)


@pytest.fixture
async def worker(settings: Settings, postgres_backend: PostgresBackend) -> AsyncIterator[None]:
    """``serve`` running on empty tables, stopped and awaited when the test ends."""
    stop = asyncio.Event()
    task = asyncio.create_task(serve(settings, stop, consumer_name="worker-it"))
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=35)


async def _settled(queue: RedisCommentQueue) -> None:
    """Until the group's backlog (new and pending entries) is empty."""

    async def empty() -> bool:
        return await queue.backlog() == 0

    await until(empty, within_s=15, every_s=0.05)


async def _statuses(engine: AsyncEngine) -> dict[str, CommentStatus]:
    comments = tables.comments
    async with engine.connect() as connection:
        rows = (
            await connection.execute(select(comments.c.external_comment_id, comments.c.status))
        ).all()
    return {row.external_comment_id: CommentStatus(row.status) for row in rows}


async def _reserved(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        reserved = await connection.scalar(select(func.sum(tables.products.c.reserved)))
    return int(reserved or 0)


async def test_queued_comments_are_processed_and_every_delivery_acknowledged(
    postgres_backend: PostgresBackend,
    migrator_engine: AsyncEngine,
    queue: RedisCommentQueue,
    redis_client: Redis,
    worker: None,
) -> None:
    session = await postgres_backend.add_live_session()
    await postgres_backend.add_product(session.id, "B07", stock_total=10)
    buyers = [
        queued_comment("je prends B07", external_comment_id=f"c-{n}", author=f"buyer-{n}")
        for n in range(5)
    ]
    rejected = queued_comment("trop beau le B07", external_comment_id="c-no-intent")
    stray = queued_comment(external_comment_id="c-stray", external_live_id="live-unknown")

    # The platform sends the first comment twice: the inbox makes it one.
    await queue.enqueue([*buyers, rejected, stray, buyers[0]])
    await _settled(queue)

    statuses = await _statuses(migrator_engine)
    assert statuses == {
        **{f"c-{n}": CommentStatus.PROCESSED for n in range(5)},
        "c-no-intent": CommentStatus.REJECTED,
        "c-stray": CommentStatus.REJECTED,
    }
    assert await _reserved(migrator_engine) == 5
    assert await postgres_backend.count("orders") == 5
    pending: Any = await redis_client.xpending(queue.stream_key, GROUP)
    assert pending["pending"] == 0


async def test_a_lock_timeout_is_retried_in_the_process(
    postgres_backend: PostgresBackend,
    migrator_engine: AsyncEngine,
    queue: RedisCommentQueue,
    redis_client: Redis,
    worker: None,
) -> None:
    session = await postgres_backend.add_live_session()
    product = await postgres_backend.add_product(session.id, "B07", stock_total=10)

    with structlog.testing.capture_logs() as logs:
        async with migrator_engine.begin() as connection:
            # Another transaction holds the product's row for 3.5 s, longer than
            # lock_timeout (2 s): the first unit 3 times out after 2 s, and its
            # retry gets the row once this transaction ends.
            await connection.execute(
                select(tables.products.c.id)
                .where(tables.products.c.id == product.id)
                .with_for_update()
            )
            await queue.enqueue([queued_comment("je prends B07")])
            await asyncio.sleep(3.5)
        await _settled(queue)

    assert (await _statuses(migrator_engine))["comment-1"] is CommentStatus.PROCESSED
    assert await _reserved(migrator_engine) == 1
    retries = [log for log in logs if log["event"] == "comment_retrying"]
    assert [log["retry"] for log in retries] == [1]
    assert retries[0]["error"].startswith("database unavailable")
    handled = [log for log in logs if log["event"] == "comment_handled"]
    assert [(log["delivery_count"], log["handling"]) for log in handled] == [(1, "processed")]
    pending: Any = await redis_client.xpending(queue.stream_key, GROUP)
    assert pending["pending"] == 0
