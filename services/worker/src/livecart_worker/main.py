"""Composition root of the worker process, started as ``livecart-worker``.

The worker's background loops run in one TaskGroup; the heartbeat is the only
one so far. SIGTERM (``docker stop``) and SIGINT (Ctrl+C) set a stop event that
every loop watches, so the process finishes its current step, releases its
connections and exits with status 0.
"""

import asyncio
import signal
from collections.abc import Callable
from functools import partial

import structlog

from livecart_core.infrastructure.redis_client import create_redis_client, ping_redis
from livecart_core.logging import configure_logging
from livecart_core.settings import Settings
from livecart_worker.heartbeat import HEARTBEAT_INTERVAL_S, run_heartbeat

logger = structlog.get_logger(__name__)

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


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
) -> None:
    """Run every worker loop until ``stop`` is set."""
    redis = create_redis_client(settings.redis_url)
    try:
        async with asyncio.TaskGroup() as loops:
            loops.create_task(
                run_heartbeat(
                    ping=partial(ping_redis, redis),
                    path=settings.worker_heartbeat_file,
                    stop=stop,
                    interval_s=heartbeat_interval_s,
                )
            )
    finally:
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
