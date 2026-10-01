"""``ShipmentRepository`` on PostgreSQL: one shipment per order, and the leases of its tracking.

Every method is one READ COMMITTED transaction of SQLAlchemy Core constructs:

- ``add`` is the plan's idempotent creation, on ``uq_shipments_order_id``::

      INSERT INTO shipments (...) VALUES (...)
      ON CONFLICT (order_id) DO NOTHING RETURNING ...

  then, when no row came back, the shipment the order has already, read by a
  statement of its own, which sees what another transaction committed
  meanwhile: of two creations at once, the second waits for the first to
  commit, then finds its row (``NOOP``). The first shipment recorded stays.
  An order that does not exist breaks the foreign key (``RecordNotFound``);
  a tracking number another order's shipment has already breaks
  ``uq_shipments_tracking_number``, a carrier's bug, and goes on as it is;
- ``unshipped_orders``, the fallback's claim::

      SELECT orders.* FROM orders
      WHERE status = 'paid' AND shipping_address IS NOT NULL AND paid_at < :paid_before
        AND NOT EXISTS (SELECT 1 FROM shipments WHERE shipments.order_id = orders.id)
      ORDER BY id LIMIT :limit FOR UPDATE OF orders SKIP LOCKED

  an anti-join on the unique index of ``shipments.order_id``. It reads every
  paid order, which the MVP's volumes allow; an order a unit holds (the
  registry's settlement that pays it) is skipped, never waited for;
- ``lease_for_tracking``, the plan's lease, in one statement::

      UPDATE shipments SET tracking_lease_until = :now + :lease FROM orders
      WHERE shipments.id IN (SELECT candidate.id FROM shipments AS candidate
                             WHERE candidate.status IN ('created', 'in_transit',
                                                        'out_for_delivery')
                               AND (candidate.tracking_lease_until IS NULL
                                    OR candidate.tracking_lease_until < :now)
                             ORDER BY candidate.id LIMIT :limit
                             FOR UPDATE SKIP LOCKED)
        AND orders.id = shipments.order_id
      RETURNING shipments.*, orders.session_id, orders.reference

  over ``ix_shipments_in_flight``. The subquery names ``shipments`` by an
  alias, so that it is not correlated with the table the statement updates;
- ``set_status`` is the compare-and-set of every machine, ``UPDATE shipments
  SET status = :target, last_event_at = :event_at WHERE id = :id AND status
  IN (the statuses the machine leaves for :target) RETURNING ...``; no row,
  and the shipment is read back to tell a repeat (``NOOP``) from a move the
  machine refuses (``InvalidTransition``). Statuses only move forward, so a
  read that shows the target reachable after all, a status a concurrent
  change passed, tries the update again, at most once per status.
"""

from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import (
    CarrierName,
    LeasedShipment,
    OrderRecord,
    ShipmentRecord,
    ShipmentRef,
    ShipmentTransition,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.reference import OrderReference
from livecart_core.domain.shipments import IN_FLIGHT
from livecart_core.domain.state_machines import (
    SHIPMENT_TRANSITIONS,
    OrderStatus,
    ShipmentStatus,
    TransitionOutcome,
    transition,
)
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.orders import order_record
from livecart_core.infrastructure.db.tables import orders, shipments
from livecart_core.infrastructure.db.units import transaction, violated_constraint

ORDER_CONSTRAINT: Final = "uq_shipments_order_id"
ORDER_FOREIGN_KEY: Final = "fk_shipments_order_id_orders"


def shipment_record(row: Row[Any]) -> ShipmentRecord:
    return ShipmentRecord(
        id=row.id,
        order_id=row.order_id,
        ref=ShipmentRef(
            CarrierName(row.carrier), row.carrier_ref, row.tracking_number, row.carrier_created_at
        ),
        status=ShipmentStatus(row.status),
        last_event_at=row.last_event_at,
    )


class SqlShipmentRepository:
    """The shipments of the paid orders; see the module docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, order_id: UUID, ref: ShipmentRef) -> ShipmentTransition:
        try:
            async with transaction(self._engine) as connection:
                row = (
                    await connection.execute(
                        insert(shipments)
                        .values(
                            id=new_id(),
                            order_id=order_id,
                            carrier=ref.carrier.value,
                            carrier_ref=ref.carrier_ref,
                            tracking_number=ref.tracking_number,
                            status=ShipmentStatus.CREATED.value,
                            carrier_created_at=ref.created_at,
                            last_event_at=ref.created_at,
                        )
                        .on_conflict_do_nothing(constraint=ORDER_CONSTRAINT)
                        .returning(*shipments.c)
                    )
                ).one_or_none()
                if row is not None:
                    return ShipmentTransition(shipment_record(row), TransitionOutcome.APPLIED)
                # The order has its shipment already, maybe committed while this insert waited.
                known = (
                    await connection.execute(
                        select(shipments).where(shipments.c.order_id == order_id)
                    )
                ).one()
        except IntegrityError as error:
            if violated_constraint(error) == ORDER_FOREIGN_KEY:
                raise RecordNotFound(f"no order {order_id}") from error
            raise
        return ShipmentTransition(shipment_record(known), TransitionOutcome.NOOP)

    async def get_by_order(self, order_id: UUID) -> ShipmentRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(select(shipments).where(shipments.c.order_id == order_id))
            ).one_or_none()
        return None if row is None else shipment_record(row)

    async def unshipped_orders(
        self, *, paid_before: datetime, limit: int
    ) -> tuple[OrderRecord, ...]:
        if limit < 1:
            raise ValueError(f"limit must be positive, not {limit}")
        shipped = select(shipments.c.id).where(shipments.c.order_id == orders.c.id).exists()
        async with transaction(self._engine) as connection:
            rows = (
                await connection.execute(
                    select(orders)
                    .where(
                        orders.c.status == OrderStatus.PAID.value,
                        orders.c.shipping_address.is_not(None),
                        orders.c.paid_at < paid_before,
                        ~shipped,
                    )
                    .order_by(orders.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True, of=orders)
                )
            ).all()
        return tuple(order_record(row) for row in rows)

    async def lease_for_tracking(
        self, *, now: datetime, limit: int, lease: timedelta
    ) -> tuple[LeasedShipment, ...]:
        if limit < 1:
            raise ValueError(f"limit must be positive, not {limit}")
        if lease <= timedelta(0):
            raise ValueError(f"lease must be positive, not {lease}")
        candidate = shipments.alias("candidate")
        leasable = (
            select(candidate.c.id)
            .where(
                candidate.c.status.in_([status.value for status in IN_FLIGHT]),
                candidate.c.tracking_lease_until.is_(None)
                | (candidate.c.tracking_lease_until < now),
            )
            .order_by(candidate.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        until = now + lease
        async with transaction(self._engine) as connection:
            rows = (
                await connection.execute(
                    update(shipments)
                    .where(shipments.c.id.in_(leasable), orders.c.id == shipments.c.order_id)
                    .values(tracking_lease_until=until)
                    .returning(*shipments.c, orders.c.session_id, orders.c.reference)
                )
            ).all()
        return tuple(
            LeasedShipment(
                shipment_record(row), row.session_id, OrderReference(row.reference), until
            )
            for row in sorted(rows, key=lambda row: row.id)
        )

    async def set_status(
        self, shipment_id: UUID, target: ShipmentStatus, *, event_at: datetime
    ) -> ShipmentTransition:
        sources = sorted(current.value for current, to in SHIPMENT_TRANSITIONS if to is target)
        # Each failed compare-and-set that a new read shows reachable is a status passed
        # by a concurrent change: there are no more tries than statuses.
        for _ in ShipmentStatus:
            async with transaction(self._engine) as connection:
                row = (
                    await connection.execute(
                        update(shipments)
                        .where(shipments.c.id == shipment_id, shipments.c.status.in_(sources))
                        .values(status=target.value, last_event_at=event_at)
                        .returning(*shipments.c)
                    )
                ).one_or_none()
                if row is not None:
                    return ShipmentTransition(shipment_record(row), TransitionOutcome.APPLIED)
                row = (
                    await connection.execute(select(shipments).where(shipments.c.id == shipment_id))
                ).one_or_none()
            if row is None:
                raise RecordNotFound(f"no shipment {shipment_id}")
            current = shipment_record(row)
            if transition(SHIPMENT_TRANSITIONS, current.status, target) is TransitionOutcome.NOOP:
                return ShipmentTransition(current, TransitionOutcome.NOOP)
        raise RuntimeError(f"shipment {shipment_id} kept changing status")
