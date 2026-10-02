"""The shipments of the paid orders: created after each payment, then tracked every minute.

Two paths create a paid order's shipment (``CreateShipment``), each once its
own units have committed, out of any transaction:

- the tick that paid the order: the payment events loop (``payments``) and
  the reconciliation of the expiry sweep (``sweeps``) hand ``ship_paid`` the
  orders their tick paid, once it has ended, and their shipments are
  created one order after the other, within ``SHIP_PAID_BUDGET_S`` (2 s):
  no carrier's call runs past it and no creation starts after it, so that a
  slow carrier delays the loop's next tick by that much at most, never by a
  10 s call timeout per order. The orders it leaves are logged, for the
  fallback;
- the shipments loop, every ``SHIPMENT_SYNC_INTERVAL_S`` (60 s): the
  fallback (``ShipUnshippedOrders``) ships the paid orders still without a
  shipment, those a worker stopped, or a carrier or PostgreSQL down, left
  behind after their payment.

The same loop then tracks the shipments in flight (``SyncShipmentTracking``):
each one leased to this worker for an interval, asked about at the carrier,
and each of its moves persisted by compare-and-set and published once as a
``shipment.updated``.

Each creation is logged and counted by outcome and by path
(``livecart_shipment_creations_total{outcome,via}``); each shipment the
tracking asked about is counted by outcome
(``livecart_shipment_tracking_total{outcome}``) and each transition it
applied by status (``livecart_shipment_transitions_total{status}``), and
logged. What needs a person is logged at ERROR: a paid order without an
address, a creation or a tracking that failed for another reason than an
outage; an order the budget left to the fallback is a warning
(``shipment_left_to_fallback``). The logs carry the order's id and reference
and the shipment's id, never the tracking number, which follows a parcel to
its buyer's door.
"""

import asyncio
import contextlib
from collections.abc import Iterable
from typing import Final

import structlog

from livecart_core.application.create_shipment import (
    CreatedShipment,
    CreateShipment,
    ShipmentCreationOutcome,
    ShipUnshippedOrders,
)
from livecart_core.application.dto import OrderRecord
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.application.sync_shipment_tracking import (
    SyncShipmentTracking,
    TrackedShipment,
    TrackingOutcome,
)
from livecart_worker.metrics import ShipmentPath, WorkerMetrics

logger = structlog.get_logger(__name__)

SHIPMENTS_INTERVAL_S: Final = 60.0
# The most a tick spends on the shipments of the orders it paid: the payment
# events loop's interval (payments.PAYMENT_EVENTS_INTERVAL_S). The fallback
# ships what it leaves once their SHIPMENT_FALLBACK_GRACE (30 s) has passed,
# at its next tick.
SHIP_PAID_BUDGET_S: Final = 2.0


def _log(created: CreatedShipment, via: ShipmentPath) -> None:
    order = created.order
    fields: dict[str, object] = {
        "order_id": str(order.id),
        "reference": order.reference.value,
        "via": via.value,
        "outcome": created.outcome.value,
    }
    if created.shipment is not None:
        fields |= {
            "shipment_id": str(created.shipment.id),
            "carrier": created.shipment.ref.carrier.value,
        }
    if created.error is not None:
        fields["error"] = type(created.error).__name__
    outcome = created.outcome
    if outcome is ShipmentCreationOutcome.NO_ADDRESS:
        logger.error("shipment_without_address", **fields)
    elif outcome is ShipmentCreationOutcome.FAILED:
        logger.error("shipment_creation_failed", exc_info=created.error, **fields)
    elif outcome is ShipmentCreationOutcome.RETRY:
        logger.warning("shipment_creation_deferred", **fields)
    else:
        logger.info("shipment_created", **fields)


def _record(
    created: tuple[CreatedShipment, ...], via: ShipmentPath, metrics: WorkerMetrics | None
) -> None:
    for item in created:
        _log(item, via)
        if metrics is not None:
            metrics.record_shipment_creation(item, via=via)


async def ship_paid(
    ship: CreateShipment,
    orders: Iterable[OrderRecord],
    metrics: WorkerMetrics | None = None,
    *,
    budget_s: float = SHIP_PAID_BUDGET_S,
) -> tuple[CreatedShipment, ...]:
    """The shipments of the orders a tick paid, one after the other, once the tick has ended.

    Within ``budget_s``: a carrier's call still running then is cut (a
    ``retry``), and the orders after it are left, each logged, for the
    fallback. What comes back is the creations that started, in order.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    created: list[CreatedShipment] = []
    left: list[OrderRecord] = []
    for order in orders:
        if loop.time() >= deadline:
            left.append(order)
        else:
            created.append(await ship(order, deadline=deadline))
    _record(tuple(created), ShipmentPath.PAYMENT, metrics)
    for order in left:
        logger.warning(
            "shipment_left_to_fallback",
            order_id=str(order.id),
            reference=order.reference.value,
            via=ShipmentPath.PAYMENT.value,
            budget_s=budget_s,
        )
    return tuple(created)


async def ship_unshipped_once(
    fallback: ShipUnshippedOrders, metrics: WorkerMetrics | None = None
) -> None:
    """One tick of the fallback; a claim that fails is logged, and the next tick tries again."""
    try:
        created = await fallback()
    except ServiceUnavailable as error:
        logger.warning("shipment_fallback_unavailable", error=str(error))
    except Exception:
        logger.exception("shipment_fallback_failed")
    else:
        _record(created, ShipmentPath.FALLBACK, metrics)


def _log_tracked(tracked: TrackedShipment) -> None:
    leased = tracked.leased
    fields: dict[str, object] = {
        "shipment_id": str(leased.shipment.id),
        "order_id": str(leased.shipment.order_id),
        "reference": leased.reference.value,
        "carrier": leased.shipment.ref.carrier.value,
        "outcome": tracked.outcome.value,
        "from_status": leased.shipment.status.value,
    }
    if tracked.moves:
        fields["status"] = tracked.moves[-1].status.value
        fields["transitions"] = len(tracked.moves)
    if tracked.error is not None:
        fields["error"] = type(tracked.error).__name__
    outcome = tracked.outcome
    if outcome is TrackingOutcome.FAILED:
        logger.error("shipment_tracking_failed", exc_info=tracked.error, **fields)
    elif outcome is TrackingOutcome.RETRY:
        logger.warning("shipment_tracking_deferred", **fields)
    elif tracked.moves:
        logger.info("shipment_moved", **fields)
    # A shipment where it was, or moved by another worker: counted, not logged.


async def track_once(sync: SyncShipmentTracking, metrics: WorkerMetrics | None = None) -> None:
    """One tick of the tracking; a lease that fails is logged, and the next tick tries again."""
    try:
        tick = await sync()
    except ServiceUnavailable as error:
        logger.warning("shipment_tracking_unavailable", error=str(error))
    except Exception:
        logger.exception("shipment_tracking_tick_failed")
    else:
        for tracked in tick.tracked:
            _log_tracked(tracked)
        if metrics is not None:
            metrics.record_tracking(tick)


async def run_shipments(
    fallback: ShipUnshippedOrders,
    stop: asyncio.Event,
    *,
    sync: SyncShipmentTracking | None = None,
    interval_s: float = SHIPMENTS_INTERVAL_S,
    metrics: WorkerMetrics | None = None,
) -> None:
    """The fallback then the tracking, at startup and every ``interval_s``, until ``stop``."""
    logger.info("shipments_started", interval_s=interval_s)
    while not stop.is_set():
        await ship_unshipped_once(fallback, metrics)
        if sync is not None:
            await track_once(sync, metrics)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(interval_s):
                await stop.wait()
    logger.info("shipments_stopped")
