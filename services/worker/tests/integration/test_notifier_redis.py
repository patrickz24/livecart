"""The buyer's notice end to end, on the compose Redis and PostgreSQL.

The worker (``serve``) processes a comment, and its buyer's reference, token
and link reach the development stream, the token derived from the order id,
and no log line: every record of the run is captured, the worker's own and
the standard library's. Then the two crashes between a commit and its
acknowledgment: before the notice is stamped, the redelivery sends the very
same token again; after, it sends nothing.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import OrderRecord, QueueDelivery
from livecart_core.application.ports import NullIntentFallback, ServiceUnavailable
from livecart_core.application.process_comment import Handling, ProcessComment
from livecart_core.application.reclaim_comments import ReclaimComments
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.infrastructure.comment_queue import GROUP, RedisCommentQueue
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.orders import SqlOrderRepository
from livecart_core.infrastructure.events import NullEventPublisher
from livecart_core.infrastructure.notifier import SimulatedBuyerNotifier
from livecart_core.order_tokens import OrderTokens
from livecart_core.settings import Settings
from livecart_core.testing.backends import CURRENCY, PostgresBackend
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, queued_comment
from livecart_core.testing.database import IntegrationSettings
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY
from livecart_core.testing.polling import until
from livecart_worker.main import serve

pytestmark = pytest.mark.integration

TOKENS = OrderTokens(TEST_ORDER_TOKEN_KEY)
IDLE = timedelta(milliseconds=200)


async def _notices(client: Redis, prefix: str) -> list[dict[str, str]]:
    entries: Any = await client.xrange(f"{prefix}:dev:buyer-dm")
    return [
        {key.decode(): value.decode() for key, value in fields.items()} for _, fields in entries
    ]


async def _order(backend: PostgresBackend, engine: AsyncEngine) -> OrderRecord:
    """The one order of the test."""
    async with engine.connect() as connection:
        order_id: UUID = (await connection.execute(select(tables.orders.c.id))).scalar_one()
    order = await backend.orders.get(order_id)
    assert order is not None
    return order


async def test_the_buyer_gets_the_derived_token_on_the_dev_stream_and_no_log_holds_it(
    integration_settings: IntegrationSettings,
    postgres_backend: PostgresBackend,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = await postgres_backend.add_live_session()
    await postgres_backend.add_product(session.id, "B07", stock_total=10)
    settings = Settings(
        database_url=integration_settings.database_url,
        redis_url=integration_settings.redis_url,
        redis_key_prefix=redis_key_prefix,
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        worker_heartbeat_file=tmp_path / "worker.heartbeat",
        _env_file=None,
    )
    queue = RedisCommentQueue(redis_client, prefix=redis_key_prefix)
    stop = asyncio.Event()
    # Every level of the standard library's loggers, and SQLAlchemy's statement
    # log, which its engine only writes when its own logger is set this low.
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="sqlalchemy.engine")

    with structlog.testing.capture_logs() as logs:
        worker = asyncio.create_task(serve(settings, stop, consumer_name="worker-it"))
        await queue.enqueue([queued_comment("je prends B07", author="buyer-42")])

        async def notified() -> bool:
            return bool(await _notices(redis_client, redis_key_prefix))

        await until(notified, within_s=15, every_s=0.05)
        stop.set()
        await asyncio.wait_for(worker, timeout=35)

    order = await _order(postgres_backend, migrator_engine)
    token = TOKENS.token_for(order.id)
    assert await _notices(redis_client, redis_key_prefix) == [
        {
            "order_id": str(order.id),
            "platform": "simulator",
            "recipient": "buyer-42",
            "reference": order.reference.value,
            "token": token,
            "link": f"livecart://link/{order.reference.value}#t={token}",
        }
    ]
    assert order.buyer_notified_at is not None
    assert any(log["event"] == "comment_handled" and log["notified"] for log in logs)
    # The whole run, logged at every level: the token is in none of it.
    assert logs
    assert any(record.name == "sqlalchemy.engine.Engine" for record in caplog.records)
    assert token not in repr(logs)
    assert token not in caplog.text


@pytest.fixture
async def world(
    redis_client: Redis, redis_key_prefix: str, postgres_backend: PostgresBackend
) -> tuple[RedisCommentQueue, ReclaimComments]:
    """A live session selling B07, the queue of this test, and a reclaim at a short idle."""
    session = await postgres_backend.add_live_session()
    await postgres_backend.add_product(session.id, "B07", stock_total=10)
    queue = RedisCommentQueue(redis_client, prefix=redis_key_prefix)
    reclaim = ReclaimComments(
        queue, postgres_backend.comments, SystemClock(), min_idle=IDLE, max_deliveries=3
    )
    return queue, reclaim


def _process(
    backend: PostgresBackend,
    queue: RedisCommentQueue,
    redis: Redis,
    prefix: str,
    orders: SqlOrderRepository,
) -> ProcessComment:
    return ProcessComment(
        inbox=backend.comments,
        sessions=backend.live_sessions,
        orders=orders,
        queue=queue,
        events=NullEventPublisher(),
        notifier=SimulatedBuyerNotifier(
            redis, prefix=prefix, link_scheme="livecart", production=False
        ),
        access_token=TOKENS.token_for,
        fallback=NullIntentFallback(),
        clock=SystemClock(),
        lease=timedelta(seconds=30),
        db_budget=timedelta(seconds=5),
        reservation_ttl=timedelta(seconds=900),
        max_quantity=DEMO_MAX_QUANTITY,
    )


class _CrashBeforeTheStamp(SqlOrderRepository):
    """The worker dies once between the notice and its stamp."""

    crashes = 1

    async def mark_buyer_notified(self, order_id: UUID, *, now: datetime) -> OrderRecord:
        if self.crashes:
            self.crashes -= 1
            raise ServiceUnavailable("the worker died")
        return await super().mark_buyer_notified(order_id, now=now)


class _CrashBeforeTheAcknowledgment(RedisCommentQueue):
    """The worker dies once after the stamp, before its XACK."""

    crashes = 1

    async def ack(self, delivery: QueueDelivery) -> None:
        if self.crashes:
            self.crashes -= 1
            raise RuntimeError("the worker died")
        await super().ack(delivery)


async def _redelivered(queue: RedisCommentQueue, reclaim: ReclaimComments) -> QueueDelivery:
    """The delivery the crashed consumer left, reclaimed by another once idle."""
    await asyncio.sleep(IDLE.total_seconds() * 2)
    reclaimed = await reclaim("worker-2", room=1)
    [delivery] = reclaimed.redelivered
    assert delivery.delivery_count == 2
    return delivery


@pytest.mark.parametrize("stamped", [False, True])
async def test_a_redelivery_after_a_crash_before_xack_notifies_again_only_without_a_stamp(
    world: tuple[RedisCommentQueue, ReclaimComments],
    postgres_backend: PostgresBackend,
    migrator_engine: AsyncEngine,
    redis_client: Redis,
    redis_key_prefix: str,
    app_engine: AsyncEngine,
    stamped: bool,
) -> None:
    queue, reclaim = world
    orders = SqlOrderRepository(app_engine, currency=CURRENCY, access_token_hash=TOKENS.stored_hash)
    crashing_orders = (
        orders
        if stamped
        else _CrashBeforeTheStamp(
            app_engine, currency=CURRENCY, access_token_hash=TOKENS.stored_hash
        )
    )
    crashing_queue = (
        _CrashBeforeTheAcknowledgment(redis_client, prefix=redis_key_prefix) if stamped else queue
    )
    await queue.enqueue([queued_comment("je prends B07")])
    [first] = await queue.receive("worker-1", max_messages=1, block_seconds=0)
    with pytest.raises((ServiceUnavailable, RuntimeError), match="died"):
        await _process(
            postgres_backend, crashing_queue, redis_client, redis_key_prefix, crashing_orders
        )(first, consumer="worker-1")

    handled = await _process(postgres_backend, queue, redis_client, redis_key_prefix, orders)(
        await _redelivered(queue, reclaim), consumer="worker-2"
    )

    order = await _order(postgres_backend, migrator_engine)
    notices = await _notices(redis_client, redis_key_prefix)
    assert handled.handling is Handling.DUPLICATE
    assert handled.notified is not stamped
    # Stamped: one notice. Not stamped: the same one twice, never two tokens.
    assert len(notices) == (1 if stamped else 2)
    assert {notice["token"] for notice in notices} == {TOKENS.token_for(order.id)}
    assert order.buyer_notified_at is not None
    pending: Any = await redis_client.xpending(queue.stream_key, GROUP)
    assert pending["pending"] == 0
