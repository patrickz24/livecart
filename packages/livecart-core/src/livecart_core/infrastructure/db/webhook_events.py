"""The registry of payment webhooks, ``processed_webhook_events``, on PostgreSQL.

Every method is one READ COMMITTED transaction of SQLAlchemy Core constructs.

``record`` is what the payment webhook writes once the provider's signature
holds, and all it writes::

    INSERT INTO processed_webhook_events (id, provider, event_id, event_type, payload, ...)
    VALUES (...)
    ON CONFLICT (provider, event_id)
    DO UPDATE SET delivery_count = processed_webhook_events.delivery_count + 1
    RETURNING delivery_count

The unique constraint ``uq_processed_webhook_events_provider_event_id`` makes
the registry the durable idempotence of the payment webhooks: a provider's
redelivery, or two deliveries of one event racing each other, end on the one
row, which counts them. ``delivery_count`` is 1 on the row this statement
inserted and more on a redelivery, which keeps the first payload, type and
processing. The payload is ``PaymentEvent``, the allow-list the provider's
adapter reduced the event to, never the provider's body.

``claim`` takes the worker's next batch, oldest first, as the plan's loop does::

    SELECT id, attempts FROM processed_webhook_events
    WHERE processed_at IS NULL AND (claimed_until IS NULL OR claimed_until < :now)
    ORDER BY created_at, id LIMIT :limit FOR UPDATE SKIP LOCKED

then, in the same transaction, settles as ``failed`` the rows that made
``max_attempts`` attempts already, and counts one attempt more on the others,
leased until ``now + lease``. The partial index
``ix_processed_webhook_events_unprocessed_created_at`` serves the scan. Two
claims at once skip each other's rows, and the lease keeps a claimed row from
the next claims once the transaction has released it.

``settle`` applies one event, in this order, each lock before the next:

1. the event's row, ``FOR UPDATE``, still unprocessed: otherwise another
   claim applied it, and nothing is written;
2. the attempt whose ``provider_ref`` is the event's checkout session, read,
   then its order, ``FOR UPDATE``, then the attempt, ``FOR UPDATE``:
   ``decide_payment`` reads both statuses under the locks;
3. the compare-and-sets it decided: ``UPDATE payments SET status = ...
   WHERE id = :id AND status = :current``, then, for a payment, ``UPDATE
   products SET reserved = reserved - :q, sold = sold + :q``, and last
   ``UPDATE orders SET status = ..., paid_at = :now WHERE id = :id AND status
   = :current``;
4. the event's row, processed with the settlement's outcome.

The order is locked before anything is written, as ``open_attempt`` locks it
first, and its status changes after the product's counters, as the expiry
sweep changes it after the release: a reservation of the same buyer and
product meanwhile finds the order still pending in
``uq_orders_pending_buyer_product`` at once, rather than wait for this unit
while it waits for the product.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livecart_core.application.dto import (
    EventClaim,
    OrderRecord,
    PaymentEvent,
    PaymentProviderName,
    PaymentSettlement,
    RegisteredEvent,
    SettlementKind,
    StockLevel,
    WebhookOutcome,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.payments import PaymentDecision, PaymentEffect, decide_payment
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.orders import order_record
from livecart_core.infrastructure.db.products import stock_level
from livecart_core.infrastructure.db.tables import (
    orders,
    payments,
    processed_webhook_events,
    products,
)
from livecart_core.infrastructure.db.units import transaction

EVENT_KEY: Final = "uq_processed_webhook_events_provider_event_id"


def registered_event(row: Row[Any]) -> RegisteredEvent:
    return RegisteredEvent(
        PaymentEvent.model_validate(row.payload),
        received_at=row.created_at,
        delivery_count=row.delivery_count,
        attempts=row.attempts,
        claimed_until=row.claimed_until,
        processed_at=row.processed_at,
        outcome=None if row.outcome is None else WebhookOutcome(row.outcome),
    )


def _oldest_first(rows: Sequence[Row[Any]]) -> tuple[RegisteredEvent, ...]:
    return tuple(registered_event(row) for row in sorted(rows, key=lambda r: (r.created_at, r.id)))


class SqlWebhookEventRegistry:
    """The registry of payment webhooks; see the module docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, event: PaymentEvent, *, now: datetime) -> bool:
        table = processed_webhook_events
        statement = (
            insert(table)
            .values(
                id=new_id(),
                provider=event.provider.value,
                event_id=event.event_id,
                event_type=event.event_type,
                payload=event.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint=EVENT_KEY,
                # ON CONFLICT runs no Python-side onupdate: updated_at is set here.
                set_={"delivery_count": table.c.delivery_count + 1, "updated_at": now},
            )
            .returning(table.c.delivery_count)
        )
        async with transaction(self._engine) as connection:
            delivery_count = (await connection.execute(statement)).scalar_one()
        return bool(delivery_count == 1)

    async def claim(
        self, *, now: datetime, limit: int, lease: timedelta, max_attempts: int
    ) -> EventClaim:
        if limit < 1 or max_attempts < 1:
            raise ValueError("a claim takes one event at least, of one attempt at least")
        if lease <= timedelta(0):
            raise ValueError(f"lease must be positive, not {lease}")
        table = processed_webhook_events
        free = table.c.processed_at.is_(None) & (
            table.c.claimed_until.is_(None) | (table.c.claimed_until < now)
        )
        async with transaction(self._engine) as connection:
            locked = (
                await connection.execute(
                    select(table.c.id, table.c.attempts)
                    .where(free)
                    .order_by(table.c.created_at, table.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            spent = [row.id for row in locked if row.attempts >= max_attempts]
            tried = [row.id for row in locked if row.attempts < max_attempts]
            exhausted: Sequence[Row[Any]] = ()
            claimed: Sequence[Row[Any]] = ()
            if spent:
                exhausted = (
                    await connection.execute(
                        update(table)
                        .where(table.c.id.in_(spent))
                        .values(processed_at=now, outcome=WebhookOutcome.FAILED.value)
                        .returning(*table.c)
                    )
                ).all()
            if tried:
                claimed = (
                    await connection.execute(
                        update(table)
                        .where(table.c.id.in_(tried))
                        .values(attempts=table.c.attempts + 1, claimed_until=now + lease)
                        .returning(*table.c)
                    )
                ).all()
        return EventClaim(_oldest_first(claimed), _oldest_first(exhausted))

    async def settle(
        self, event: PaymentEvent, effect: PaymentEffect, *, now: datetime
    ) -> PaymentSettlement | None:
        async with transaction(self._engine) as connection:
            event_row_id = await self._lock_unprocessed(connection, event.provider, event.event_id)
            if event_row_id is None:
                return None
            settlement = await self._settle(connection, event, effect, now=now)
            await connection.execute(
                update(processed_webhook_events)
                .where(processed_webhook_events.c.id == event_row_id)
                .values(processed_at=now, outcome=settlement.kind.outcome.value)
            )
        return settlement

    async def mark_processed(
        self,
        provider: PaymentProviderName,
        event_id: str,
        outcome: WebhookOutcome,
        *,
        now: datetime,
    ) -> bool:
        table = processed_webhook_events
        async with transaction(self._engine) as connection:
            marked = (
                await connection.execute(
                    update(table)
                    .where(
                        table.c.provider == provider.value,
                        table.c.event_id == event_id,
                        table.c.processed_at.is_(None),
                    )
                    .values(processed_at=now, outcome=outcome.value)
                    .returning(table.c.id)
                )
            ).one_or_none()
            if marked is None and await self._find(connection, provider, event_id) is None:
                raise RecordNotFound(f"no {provider} webhook event {event_id}")
        return marked is not None

    # --- The steps of settle -----------------------------------------------------------

    async def _lock_unprocessed(
        self, connection: AsyncConnection, provider: PaymentProviderName, event_id: str
    ) -> UUID | None:
        """The id of the event's row, locked, while unprocessed; ``None`` once processed."""
        table = processed_webhook_events
        row_id: UUID | None = await connection.scalar(
            select(table.c.id)
            .where(
                table.c.provider == provider.value,
                table.c.event_id == event_id,
                table.c.processed_at.is_(None),
            )
            .with_for_update()
        )
        if row_id is None and await self._find(connection, provider, event_id) is None:
            raise RecordNotFound(f"no {provider} webhook event {event_id}")
        return row_id

    @staticmethod
    async def _find(
        connection: AsyncConnection, provider: PaymentProviderName, event_id: str
    ) -> UUID | None:
        table = processed_webhook_events
        found: UUID | None = await connection.scalar(
            select(table.c.id).where(
                table.c.provider == provider.value, table.c.event_id == event_id
            )
        )
        return found

    async def _settle(
        self,
        connection: AsyncConnection,
        event: PaymentEvent,
        effect: PaymentEffect,
        *,
        now: datetime,
    ) -> PaymentSettlement:
        if event.checkout_ref is None:
            return PaymentSettlement(SettlementKind.UNKNOWN_SESSION)
        attempt = (
            await connection.execute(
                select(payments.c.id, payments.c.order_id).where(
                    payments.c.provider_ref == event.checkout_ref,
                    payments.c.provider == event.provider.value,
                )
            )
        ).one_or_none()
        if attempt is None:
            return PaymentSettlement(SettlementKind.UNKNOWN_SESSION)
        order = (
            await connection.execute(
                select(orders).where(orders.c.id == attempt.order_id).with_for_update()
            )
        ).one()
        payment_status = PaymentStatus(
            (
                await connection.execute(
                    select(payments.c.status).where(payments.c.id == attempt.id).with_for_update()
                )
            ).scalar_one()
        )
        record = order_record(order)
        decision = decide_payment(
            effect,
            payment=payment_status,
            order=record.status,
            charged=event.amount,
            due=record.total,
        )
        kind = SettlementKind.of(decision.verdict)
        if kind is not SettlementKind.APPLIED:
            return PaymentSettlement(kind, record)
        await self._move_payment(connection, attempt.id, payment_status, decision, event)
        stock = None
        if decision.order is OrderStatus.PAID:
            stock = await self._sell(connection, order.product_id, order.quantity)
        if decision.order is not None:
            record = await self._move_order(connection, record.id, record.status, decision, now)
        return PaymentSettlement(
            kind,
            record,
            order_changed=decision.order is not None,
            payment_changed=decision.payment is not None,
            stock=stock,
        )

    @staticmethod
    async def _move_payment(
        connection: AsyncConnection,
        payment_id: UUID,
        current: PaymentStatus,
        decision: PaymentDecision,
        event: PaymentEvent,
    ) -> None:
        if decision.payment is None:
            return
        moved = await connection.execute(
            update(payments)
            .where(payments.c.id == payment_id, payments.c.status == current.value)
            .values(
                status=decision.payment.value,
                provider_payment_ref=func.coalesce(
                    event.payment_ref, payments.c.provider_payment_ref
                ),
            )
        )
        if moved.rowcount != 1:
            # Impossible while this transaction holds the attempt's row.
            raise RuntimeError(f"payment {payment_id} changed under its lock")

    @staticmethod
    async def _sell(connection: AsyncConnection, product_id: UUID, quantity: int) -> StockLevel:
        """The order's reserved units, sold: the counters of its product after the move."""
        counters = (
            await connection.execute(
                update(products)
                .where(products.c.id == product_id)
                .values(reserved=products.c.reserved - quantity, sold=products.c.sold + quantity)
                .returning(products.c.stock_total, products.c.reserved, products.c.sold)
            )
        ).one()
        return stock_level(counters)

    @staticmethod
    async def _move_order(
        connection: AsyncConnection,
        order_id: UUID,
        current: OrderStatus,
        decision: PaymentDecision,
        now: datetime,
    ) -> OrderRecord:
        if decision.order is None:
            raise ValueError("no order status to move to")
        moved = (
            await connection.execute(
                update(orders)
                .where(orders.c.id == order_id, orders.c.status == current.value)
                .values(status=decision.order.value, paid_at=now)
                .returning(*orders.c)
            )
        ).one_or_none()
        if moved is None:
            raise RuntimeError(f"order {order_id} changed under its lock")
        return order_record(moved)
