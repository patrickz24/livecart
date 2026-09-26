"""Composition root of the worker process, started as ``livecart-worker``.

``serve`` builds the adapters once per process (one engine, whose pool fits
``WORKER_DB_MAX_CONNECTIONS``, one Redis client) and the use cases over them,
then runs the worker's loops in one TaskGroup:

- the comment consumer (``consumer.CommentConsumer`` over ``ProcessComment``);
- the reclaim loop, every 30 s (``reclaim``): deliveries left idle by another
  consumer redelivered or dead-lettered, then the safe trim of the stream;
- the expiry sweep, every 5 s (``sweeps``): the reservations nobody paid in
  time expired, and their stock released;
- the heartbeat, which also checks that the consumer keeps going round.

SIGTERM (``docker stop``) and SIGINT (Ctrl+C) set a stop event that every
loop watches: the consumer stops reading, lets its handlers finish (each by
its deadline), and the process releases its connections and exits with
status 0. A delivery cut short is only left pending, never lost.
"""

import asyncio
import signal
from collections.abc import Callable
from functools import partial
from typing import Final

import structlog

from livecart_core.application.expire_reservations import ExpireReservations
from livecart_core.application.ports import Clock
from livecart_core.application.process_comment import ProcessComment
from livecart_core.application.reclaim_comments import ReclaimComments
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.infrastructure.comment_queue import RedisCommentQueue
from livecart_core.infrastructure.db.comments import SqlCommentInbox
from livecart_core.infrastructure.db.engine import create_db_engine
from livecart_core.infrastructure.db.live_sessions import SqlLiveSessionRepository
from livecart_core.infrastructure.db.orders import SqlOrderRepository
from livecart_core.infrastructure.events import NullEventPublisher
from livecart_core.infrastructure.notifier import SimulatedBuyerNotifier
from livecart_core.infrastructure.redis_client import create_redis_client, ping_redis
from livecart_core.logging import configure_logging
from livecart_core.order_tokens import OrderTokens
from livecart_core.settings import Settings
from livecart_worker.consumer import CommentConsumer, default_consumer_name
from livecart_worker.heartbeat import HEARTBEAT_INTERVAL_S, run_heartbeat
from livecart_worker.reclaim import RECLAIM_INTERVAL_S, run_reclaim
from livecart_worker.sweeps import EXPIRY_BUDGET, EXPIRY_INTERVAL_S, run_expiry_sweep

logger = structlog.get_logger(__name__)

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
APPLICATION_NAME: Final = "livecart-worker"
# How long the consumer may go without a round of its loop before the
# heartbeat stops beating: it goes round at least every RECEIVE_BLOCK_S (2 s).
CONSUMER_STALL_S: Final = 30.0


def install_stop_handlers(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> Callable[[], None]:
    """Make SIGTERM and SIGINT set ``stop``; return a function that restores the defaults."""
    try:
        for sig in STOP_SIGNALS:
            loop.add_signal_handler(sig, stop.set)
    except NotImplementedError:
        # Windows event loops lack add_signal_handler; asyncio still wakes up on signals.
        def on_signal(_signum: int, _frame: object) -> None:
            loop.call_soon_threadsafe(stop.set)

        previous = {sig: signal.signal(sig, on_signal) for sig in STOP_SIGNALS}

        def restore_signal_handlers() -> None:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

        return restore_signal_handlers

    def remove_signal_handlers() -> None:
        for sig in STOP_SIGNALS:
            loop.remove_signal_handler(sig)

    return remove_signal_handlers


async def serve(
    settings: Settings,
    stop: asyncio.Event,
    *,
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
    clock: Clock | None = None,
    consumer_name: str | None = None,
    reclaim_interval_s: float = RECLAIM_INTERVAL_S,
    expiry_interval_s: float = EXPIRY_INTERVAL_S,
) -> None:
    """Run every worker loop until ``stop`` is set.

    ``clock`` and ``consumer_name`` default to the system clock and to
    ``<host>-<pid>``; the tests fix them, and the intervals of the loops.
    """
    clock = clock if clock is not None else SystemClock()
    redis = create_redis_client(settings.redis_url)
    engine = create_db_engine(
        settings.database_url,
        application_name=APPLICATION_NAME,
        pool_size=settings.worker_pool.size,
        max_overflow=settings.worker_pool.max_overflow,
    )
    try:
        queue = RedisCommentQueue(redis, prefix=settings.redis_key_prefix)
        inbox = SqlCommentInbox(engine)
        tokens = OrderTokens(settings.order_token_key)
        orders = SqlOrderRepository(
            engine, currency=settings.default_currency, access_token_hash=tokens.stored_hash
        )
        events = NullEventPublisher()
        process = ProcessComment(
            inbox=inbox,
            sessions=SqlLiveSessionRepository(engine),
            orders=orders,
            queue=queue,
            events=events,
            # Refused at startup in production: see livecart_core.infrastructure.notifier.
            notifier=SimulatedBuyerNotifier(
                redis,
                prefix=settings.redis_key_prefix,
                link_scheme=settings.buyer_link_scheme,
                production=settings.is_production,
            ),
            access_token=tokens.token_for,
            clock=clock,
            lease=settings.message_process_timeout,
            reservation_ttl=settings.reservation_ttl,
            max_quantity=settings.max_quantity_per_comment,
        )
        consumer = CommentConsumer(
            queue,
            process,
            name=consumer_name if consumer_name is not None else default_consumer_name(),
            batch=settings.worker_batch,
            concurrency=settings.worker_concurrency,
            deadline_s=settings.message_process_timeout_s,
        )
        reclaim = ReclaimComments(
            queue,
            inbox,
            clock,
            min_idle=settings.queue_reclaim_min_idle,
            max_deliveries=settings.queue_max_deliveries,
        )
        expire = ExpireReservations(orders, events, clock, budget=EXPIRY_BUDGET)
        async with asyncio.TaskGroup() as loops:
            loops.create_task(consumer.run(stop))
            loops.create_task(run_reclaim(reclaim, consumer, stop, interval_s=reclaim_interval_s))
            loops.create_task(run_expiry_sweep(expire, stop, interval_s=expiry_interval_s))
            loops.create_task(
                run_heartbeat(
                    ping=partial(ping_redis, redis),
                    path=settings.worker_heartbeat_file,
                    stop=stop,
                    interval_s=heartbeat_interval_s,
                    progressing=partial(consumer.progressed_within, CONSUMER_STALL_S),
                )
            )
    finally:
        await engine.dispose()
        await redis.aclose()


async def run(settings: Settings) -> None:
    stop = asyncio.Event()
    restore_signal_handlers = install_stop_handlers(asyncio.get_running_loop(), stop)
    logger.info("worker_started")
    try:
        await serve(settings, stop)
    finally:
        restore_signal_handlers()
        logger.info("worker_stopped")


def main() -> None:
    settings = Settings()
    configure_logging(service="worker", level=settings.log_level)
    asyncio.run(run(settings))
