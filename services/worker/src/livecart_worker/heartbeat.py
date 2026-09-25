"""Liveness heartbeat of the worker.

The loop pings Redis every ``interval_s``, and only a successful PING touches the
heartbeat file. ``livecart-healthcheck heartbeat`` then reports the container
unhealthy once the file is older than three intervals, whether Redis is out of
reach or the event loop is stuck. The loop needs no database.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL_S = 10.0
PING_TIMEOUT_S = 2.0


def _touch(path: Path) -> None:
    # A metadata update on a local tmpfs, done inline rather than in a thread:
    # a thread still running after cancellation could re-create the file that
    # shutdown has just removed.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


async def _ping_succeeds(ping: Callable[[], Awaitable[object]], timeout_s: float) -> bool:
    try:
        async with asyncio.timeout(timeout_s):
            await ping()
    except Exception as exc:  # includes TimeoutError
        logger.warning("heartbeat_ping_failed", error=type(exc).__name__)
        return False
    return True


async def run_heartbeat(
    *,
    ping: Callable[[], Awaitable[object]],
    path: Path,
    stop: asyncio.Event,
    interval_s: float = HEARTBEAT_INTERVAL_S,
    ping_timeout_s: float = PING_TIMEOUT_S,
) -> None:
    """Beat until ``stop`` is set, then remove the file so no stale beat survives."""
    logger.info("heartbeat_started", path=str(path), interval_s=interval_s)
    try:
        while not stop.is_set():
            if await _ping_succeeds(ping, ping_timeout_s):
                _touch(path)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(interval_s):
                    await stop.wait()
    finally:
        # Synchronous too: it must complete even while the task is cancelled.
        _remove(path)
        logger.info("heartbeat_stopped")
