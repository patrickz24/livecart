"""The shipments of the paid orders: created after each payment, and every minute by the fallback.

Two paths create a paid order's shipment (``CreateShipment``), each once its
own units have committed, out of any transaction:

- the tick that paid the order: the payment events loop (``payments``) and
  the reconciliation of the expiry sweep (``sweeps``) hand ``ship_paid`` the
  orders their tick paid, once it has ended, and their shipments are
  created one order after the other;
- the shipments loop, every 60 s: the fallback (``ShipUnshippedOrders``)
  ships the paid orders still without a shipment, those a worker stopped,
  or a carrier or PostgreSQL down, left behind after their payment.

Each creation is logged and counted by outcome and by path
(``livecart_shipment_creations_total{outcome,via}``). What needs a person is
logged at ERROR: a paid order without an address, a creation that failed
for another reason than an outage. The logs carry the order's id and
reference and the shipment's id, never the tracking number, which follows
a parcel to its buyer's door.
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
from livecart_worker.metrics import ShipmentPath, WorkerMetrics

logger = structlog.get_logger(__name__)

SHIPMENTS_INTERVAL_S: Final = 60.0


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
    ship: CreateShipment, orders: Iterable[OrderRecord], metrics: WorkerMetrics | None = None
) -> tuple[CreatedShipment, ...]:
    """The shipments of the orders a tick paid, one after the other, once the tick has ended."""
    created = await ship.each(orders)
    _record(created, ShipmentPath.PAYMENT, metrics)
    return created


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


async def run_shipments(
    fallback: ShipUnshippedOrders,
    stop: asyncio.Event,
    *,
    interval_s: float = SHIPMENTS_INTERVAL_S,
    metrics: WorkerMetrics | None = None,
) -> None:
    """Tick at startup, then every ``interval_s``, until ``stop`` is set."""
    logger.info("shipments_started", interval_s=interval_s)
    while not stop.is_set():
        await ship_unshipped_once(fallback, metrics)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(interval_s):
                await stop.wait()
    logger.info("shipments_stopped")
