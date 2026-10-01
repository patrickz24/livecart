"""The payment events loop: every 2 s, the registry of payment webhooks applied.

One tick is ``ProcessPaymentEvents``: claims of 20 events at most, ``FOR
UPDATE SKIP LOCKED``, each event applied in a unit of its own, until a claim
comes back short or the tick's 2 s have passed. Every worker runs the loop;
their claims skip each other's events, and a claimed event is leased to its
worker, so each is applied once. The loop is sequential and holds at most
one connection at a time. A tick that fails (PostgreSQL down) is logged, and
the next one tries again: the events wait in the registry, durable, for as
long as it takes, a worker stopped for a while included.

Once a tick has ended, the orders it paid get their shipments
(``shipments.ship_paid``), one after the other, out of any transaction; one
the tick could not ship is left to the shipments loop's fallback.

What needs a person is logged at ERROR: an event whose amount or currency
is not its order's, one that failed its 10 attempts, and a payment that
reached an order after the sweep released its stock (``paid_after_expiry``,
refunded by hand: ``docs/runbooks/paid-after-expiry.md``). Every result is
counted in the worker's metrics.
"""

import asyncio
import contextlib
from datetime import timedelta
from typing import Final

import structlog

from livecart_core.application.apply_payment_events import (
    HandledPaymentEvent,
    PaymentEventResult,
    ProcessPaymentEvents,
)
from livecart_core.application.create_shipment import CreateShipment
from livecart_core.application.ports import ServiceUnavailable
from livecart_worker.metrics import WorkerMetrics
from livecart_worker.shipments import ship_paid

logger = structlog.get_logger(__name__)

PAYMENT_EVENTS_INTERVAL_S: Final = 2.0
# A tick's budget: its interval, so that a tick never runs into the next one.
PAYMENT_EVENTS_BUDGET: Final = timedelta(seconds=PAYMENT_EVENTS_INTERVAL_S)


def _log(handled: HandledPaymentEvent) -> None:
    registered, result = handled.event, handled.result
    event = registered.event
    fields: dict[str, object] = {
        "provider": event.provider.value,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "attempts": registered.attempts,
    }
    if handled.order is not None:
        fields |= {
            "order_id": str(handled.order.id),
            "reference": handled.order.reference.value,
            "order_status": handled.order.status.value,
        }
    if handled.error is not None:
        fields["error"] = type(handled.error).__name__
    if handled.paid_after_expiry:
        logger.error("payment_after_expiry", **fields)
    elif result is PaymentEventResult.AMOUNT_MISMATCH:
        charged = event.amount
        logger.error(
            "payment_event_amount_mismatch",
            charged_minor=None if charged is None else charged.amount_minor,
            charged_currency=None if charged is None else charged.currency,
            **fields,
        )
    elif result is PaymentEventResult.ATTEMPTS_EXHAUSTED:
        logger.error("payment_event_failed", **fields)
    elif result is PaymentEventResult.RETRY:
        transient = isinstance(handled.error, ServiceUnavailable)
        logger.warning(
            "payment_event_attempt_failed", exc_info=None if transient else handled.error, **fields
        )
    elif result is PaymentEventResult.UNKNOWN_SESSION:
        logger.warning("payment_event_unknown_session", checkout_ref=event.checkout_ref, **fields)
    else:
        logger.info("payment_event_handled", result=result.value, **fields)


async def process_once(
    process: ProcessPaymentEvents,
    metrics: WorkerMetrics | None = None,
    ship: CreateShipment | None = None,
) -> None:
    """One tick, then the shipments of the orders it paid; see the module docstring."""
    try:
        tick = await process()
    except ServiceUnavailable as error:
        logger.warning("payment_events_unavailable", error=str(error))
    except Exception:
        logger.exception("payment_events_failed")
    else:
        for handled in tick.handled:
            _log(handled)
        if metrics is not None:
            metrics.record_payment_events(tick)
        if ship is not None:
            paid = [handled.order for handled in tick.handled if handled.paid and handled.order]
            await ship_paid(ship, paid, metrics)


async def run_payment_events(
    process: ProcessPaymentEvents,
    stop: asyncio.Event,
    *,
    interval_s: float = PAYMENT_EVENTS_INTERVAL_S,
    metrics: WorkerMetrics | None = None,
    ship: CreateShipment | None = None,
) -> None:
    """Tick at startup, then every ``interval_s``, until ``stop`` is set."""
    logger.info("payment_events_started", interval_s=interval_s)
    while not stop.is_set():
        await process_once(process, metrics, ship)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(interval_s):
                await stop.wait()
    logger.info("payment_events_stopped")
