"""The expiry sweep loop: every 5 s, the reservations nobody paid in time, released.

One tick is ``ExpireReservations``: batches of 100 due orders, each batch
one transaction, until a batch comes back short or the tick's 5 s have
passed. Every worker runs the loop; their sweeps skip each other's orders
(``FOR UPDATE SKIP LOCKED``), so a busy deployment sweeps faster with more
workers, and never releases an order twice. The loop is sequential and
holds at most one connection at a time; a failure is logged, and the next
tick tries again. Each tick counts what it expired in the worker's metrics.
"""

import asyncio
import contextlib
from datetime import timedelta
from typing import Final

import structlog

from livecart_core.application.expire_reservations import ExpireReservations
from livecart_core.application.ports import ServiceUnavailable
from livecart_worker.metrics import WorkerMetrics

logger = structlog.get_logger(__name__)

EXPIRY_INTERVAL_S: Final = 5.0
# A tick's budget: its interval, so that a sweep never runs into the next one.
EXPIRY_BUDGET: Final = timedelta(seconds=EXPIRY_INTERVAL_S)


async def sweep_once(expire: ExpireReservations, metrics: WorkerMetrics | None = None) -> None:
    """One tick; see the module docstring."""
    try:
        result = await expire()
    except ServiceUnavailable as error:
        logger.warning("reservation_sweep_unavailable", error=str(error))
    except Exception:
        logger.exception("reservation_sweep_failed")
    else:
        if metrics is not None:
            metrics.record_sweep(result)
        if result.expired:
            logger.info(
                "reservations_expired",
                expired=result.expired,
                batches=result.batches,
                unpublished_events=result.unpublished_events,
            )


async def run_expiry_sweep(
    expire: ExpireReservations,
    stop: asyncio.Event,
    *,
    interval_s: float = EXPIRY_INTERVAL_S,
    metrics: WorkerMetrics | None = None,
) -> None:
    """Tick at startup, then every ``interval_s``, until ``stop`` is set."""
    logger.info("reservation_sweep_started", interval_s=interval_s)
    while not stop.is_set():
        await sweep_once(expire, metrics)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(interval_s):
                await stop.wait()
    logger.info("reservation_sweep_stopped")
