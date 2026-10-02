"""``CreateShipment`` and ``ShipUnshippedOrders``: a paid order's parcel, created at its carrier.

The end of step 13 of the plan's journey: once an order is paid, its
shipment is created out of any transaction, in this order:

1. the carrier's ``create_shipment``, with the order's id, reference and
   address, idempotent per order: a crash after it and before the record is
   harmless, since the next creation names the same shipment;
2. one unit, ``ShipmentRepository.add``, ``INSERT ... ON CONFLICT DO
   NOTHING`` on ``uq_shipments_order_id``: of creations at once (the
   payment's path and the fallback, two workers), one records the shipment,
   the others find it;
3. after the commit, the shipment goes to its session's stream
   (``shipment.updated``, ``created``), best effort, once: by the creation
   that recorded it.

The worker creates the shipment of every order a tick of its payment loop,
or a reconciliation of the expiry sweep, paid, once that tick has ended, one
order after the other: no connection is held during a carrier's call, and
never two at once. A deadline bounds that time, so that a slow carrier never
stalls the loop: no carrier's call runs past it, and the worker starts no
creation after it. Whatever fails then, or comes after the deadline (the
worker stops, the carrier or PostgreSQL is down, the carrier is slow), is
left to the fallback, ``ShipUnshippedOrders``, which the shipments loop runs
every minute: the paid orders still without a shipment
(``ShipmentRepository.unshipped_orders``, ``FOR UPDATE SKIP LOCKED``), paid
``SHIPMENT_FALLBACK_GRACE`` (30 s) ago at least, so that it leaves the
orders being shipped to the payment's own path, get theirs the same way. Two
creations of one shipment cost a carrier call more, never a second shipment.

A paid order without an address cannot ship. The checkout refuses to start
without one, so it needs a person (``no_address``, logged at ERROR); the
fallback never takes such an order, so it is reported once.
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final

from livecart_core.application.dto import OrderRecord, ShipmentRecord, ShipmentRequest
from livecart_core.application.ports import (
    CarrierProvider,
    Clock,
    EventPublisher,
    ServiceUnavailable,
    ShipmentRepository,
)
from livecart_core.application.session_events import shipment_updated
from livecart_core.domain.state_machines import OrderStatus, TransitionOutcome

# The longest a carrier's call may take: past it, the fallback asks again later.
CARRIER_CALL_TIMEOUT: Final = timedelta(seconds=10)
# The most orders one claim of the fallback takes, and how long after its
# payment an order is left to the path that paid it.
SHIPMENT_FALLBACK_BATCH: Final = 20
SHIPMENT_FALLBACK_GRACE: Final = timedelta(seconds=30)


class ShipmentCreationOutcome(StrEnum):
    """What creating one order's shipment came to: the label of the worker's metric."""

    # This creation recorded the order's shipment, and published it.
    CREATED = "created"
    # The order had its shipment already: another creation recorded it first.
    EXISTING = "existing"
    # A paid order without an address: it needs a person.
    NO_ADDRESS = "no_address"
    # The carrier or the database could not be reached: the fallback tries again.
    RETRY = "retry"
    # Anything else: logged at ERROR, and the fallback tries again.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CreatedShipment:
    """One paid order and what creating its shipment came to.

    ``shipment`` is the order's shipment as recorded, when there is one, and
    ``error`` what kept a ``retry`` or a ``failed`` creation from it.
    """

    order: OrderRecord
    outcome: ShipmentCreationOutcome
    shipment: ShipmentRecord | None = None
    unpublished_events: int = 0
    error: Exception | None = None


class CreateShipment:
    """Creates and records one paid order's shipment; see the module docstring."""

    def __init__(
        self,
        shipments: ShipmentRepository,
        carrier: CarrierProvider,
        events: EventPublisher,
        clock: Clock,
        *,
        call_timeout: timedelta = CARRIER_CALL_TIMEOUT,
    ) -> None:
        if call_timeout <= timedelta(0):
            raise ValueError(f"a carrier's call needs time, not {call_timeout}")
        self._shipments = shipments
        self._carrier = carrier
        self._events = events
        self._clock = clock
        self._call_timeout = call_timeout

    async def __call__(
        self, order: OrderRecord, *, deadline: float | None = None
    ) -> CreatedShipment:
        """The order's shipment; it never raises for the carrier or the database.

        ``deadline``, a time of the event loop's clock (``loop.time()``, what
        ``asyncio.timeout_at`` reads), ends the carrier's call then if
        ``call_timeout`` has not ended it before: a ``retry``, for the
        fallback.
        """
        if order.status is not OrderStatus.PAID:
            raise ValueError(f"order {order.id} is {order.status}: only a paid order ships")
        address = order.shipping_address
        if address is None:
            return CreatedShipment(order, ShipmentCreationOutcome.NO_ADDRESS)
        call_ends = asyncio.get_running_loop().time() + self._call_timeout.total_seconds()
        if deadline is not None:
            call_ends = min(call_ends, deadline)
        try:
            async with asyncio.timeout_at(call_ends):
                ref = await self._carrier.create_shipment(
                    ShipmentRequest(order.id, order.reference, address)
                )
            added = await self._shipments.add(order.id, ref)
        except (ServiceUnavailable, TimeoutError) as error:
            return CreatedShipment(order, ShipmentCreationOutcome.RETRY, error=error)
        except Exception as error:
            return CreatedShipment(order, ShipmentCreationOutcome.FAILED, error=error)
        if added.outcome is TransitionOutcome.NOOP:
            return CreatedShipment(order, ShipmentCreationOutcome.EXISTING, added.shipment)
        unpublished = await self._publish(order, added.shipment)
        return CreatedShipment(order, ShipmentCreationOutcome.CREATED, added.shipment, unpublished)

    async def each(self, orders: Iterable[OrderRecord]) -> tuple[CreatedShipment, ...]:
        """The shipment of every order, one after the other: one connection at most at a time."""
        return tuple([await self(order) for order in orders])

    async def _publish(self, order: OrderRecord, shipment: ShipmentRecord) -> int:
        """The shipment, created, on its order's session stream; 1 when that failed."""
        try:
            await self._events.publish(
                shipment_updated(
                    order.session_id, shipment, correlation_id=None, occurred_at=self._clock.now()
                )
            )
        except ServiceUnavailable:
            return 1
        return 0


class ShipUnshippedOrders:
    """One tick of the fallback: paid orders left without a shipment; see the module docstring."""

    def __init__(
        self,
        shipments: ShipmentRepository,
        create: CreateShipment,
        clock: Clock,
        *,
        batch: int = SHIPMENT_FALLBACK_BATCH,
        grace: timedelta = SHIPMENT_FALLBACK_GRACE,
    ) -> None:
        if batch < 1 or grace < timedelta(0):
            raise ValueError("a claim takes one order at least, after a grace of 0 s or more")
        self._shipments = shipments
        self._create = create
        self._clock = clock
        self._batch = batch
        self._grace = grace

    async def __call__(self) -> tuple[CreatedShipment, ...]:
        """One claim, then each order's shipment.

        A claim that fails raises (``ServiceUnavailable`` when PostgreSQL is
        down): its orders wait for the next tick. One claim a tick, so that
        orders the carrier keeps refusing are never asked about twice in it.
        """
        orders = await self._shipments.unshipped_orders(
            paid_before=self._clock.now() - self._grace, limit=self._batch
        )
        return await self._create.each(orders)
