"""The expiry sweep loop: every 5 s, the reservations nobody paid in time, released.

One tick runs the three phases of the plan's step 14, one after the other:

1. ``ExpireReservations``: batches of 100 due orders without a payment in
   flight, each batch one transaction, until a batch comes back short or
   the tick's 5 s have passed;
2. and 3. ``ExpireCheckouts``: the due orders whose attempt is still
   pending, leased 20 at a time for 30 s, so that one worker alone asks the
   provider to expire each session (expire-first), 4 calls at a time,
   holding no connection during a call. An expired session releases the
   order's stock; one the buyer paid first is settled as paid, through the
   registry of payment events; anything else waits for the next lease.

An order the reconciliation paid gets its shipment once the tick has
ended (``shipments.ship_paid``), as one the payment events loop paid does,
within the same 2 s, the rest left to the shipments loop's fallback.

Every worker runs the loop; their sweeps skip each other's orders (``FOR
UPDATE SKIP LOCKED``, and the lease), so a busy deployment sweeps faster
with more workers, and never releases an order twice. The loop holds at most
one connection at a time; a phase that fails is logged, and the next tick
tries again. Each tick counts what it settled in the worker's metrics, and
logs at ERROR what needs a person: a session the provider does not know, an
attempt of another provider, a key the provider refuses.
"""

import asyncio
import contextlib
from datetime import timedelta
from typing import Final

import structlog

from livecart_core.application.create_shipment import CreateShipment
from livecart_core.application.expire_checkouts import (
    CheckoutExpiryOutcome,
    ExpireCheckouts,
    HandledCheckout,
)
from livecart_core.application.expire_reservations import ExpireReservations
from livecart_core.application.ports import ServiceUnavailable
from livecart_worker.metrics import WorkerMetrics
from livecart_worker.shipments import ship_paid

logger = structlog.get_logger(__name__)

EXPIRY_INTERVAL_S: Final = 5.0
# A tick's budget: its interval, so that a sweep never runs into the next one.
EXPIRY_BUDGET: Final = timedelta(seconds=EXPIRY_INTERVAL_S)


def _log_checkout(handled: HandledCheckout) -> None:
    due = handled.due
    fields: dict[str, object] = {
        "order_id": str(due.order.id),
        "reference": due.order.reference.value,
        "payment_id": str(due.payment.id),
        "attempt_no": due.payment.attempt_no,
        "provider": due.payment.provider.value,
        "outcome": handled.outcome.value,
        "recreated": handled.recreated,
    }
    if handled.order is not None:
        fields["order_status"] = handled.order.status.value
    if handled.error is not None:
        fields["error"] = type(handled.error).__name__
    if handled.outcome is CheckoutExpiryOutcome.FAILED:
        logger.error("checkout_expiry_failed", exc_info=handled.error, **fields)
    elif handled.outcome is CheckoutExpiryOutcome.RETRY:
        logger.warning("checkout_expiry_deferred", **fields)
    else:
        logger.info("checkout_expiry_settled", **fields)


async def _expire_reservations(expire: ExpireReservations, metrics: WorkerMetrics | None) -> None:
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


async def _expire_checkouts(
    checkouts: ExpireCheckouts, metrics: WorkerMetrics | None, ship: CreateShipment | None
) -> None:
    try:
        tick = await checkouts()
    except ServiceUnavailable as error:
        logger.warning("checkout_expiry_unavailable", error=str(error))
    except Exception:
        logger.exception("checkout_expiry_tick_failed")
    else:
        for handled in tick.handled:
            _log_checkout(handled)
        if metrics is not None:
            metrics.record_checkout_expiry(tick)
        if ship is not None:
            paid = [handled.order for handled in tick.handled if handled.paid and handled.order]
            await ship_paid(ship, paid, metrics)


async def sweep_once(
    expire: ExpireReservations,
    metrics: WorkerMetrics | None = None,
    checkouts: ExpireCheckouts | None = None,
    ship: CreateShipment | None = None,
) -> None:
    """One tick: the first phase, then the later ones; see the module docstring."""
    await _expire_reservations(expire, metrics)
    if checkouts is not None:
        await _expire_checkouts(checkouts, metrics, ship)


async def run_expiry_sweep(
    expire: ExpireReservations,
    stop: asyncio.Event,
    *,
    checkouts: ExpireCheckouts | None = None,
    interval_s: float = EXPIRY_INTERVAL_S,
    metrics: WorkerMetrics | None = None,
    ship: CreateShipment | None = None,
) -> None:
    """Tick at startup, then every ``interval_s``, until ``stop`` is set."""
    logger.info("reservation_sweep_started", interval_s=interval_s)
    while not stop.is_set():
        await sweep_once(expire, metrics, checkouts, ship)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(interval_s):
                await stop.wait()
    logger.info("reservation_sweep_stopped")
