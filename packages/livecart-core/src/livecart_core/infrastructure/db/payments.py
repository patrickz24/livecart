"""``PaymentAttempts`` on PostgreSQL: the attempts a checkout opens, one transaction per call.

``open_attempt``, step 1 of ``StartCheckout``, runs before any call to the
provider, in one READ COMMITTED transaction of SQLAlchemy Core constructs:

1. ``SELECT ... FROM orders WHERE id = :id FOR UPDATE``: the order's row,
   locked until the transaction ends, so that the attempts of one order
   open one at a time, and a sweep expiring the order waits for this one or
   is waited for (``FOR UPDATE SKIP LOCKED`` on its side: it skips the row
   while this transaction holds it);
2. the attempts of the order, read under that lock: a pending one is
   returned as it is, the reservation untouched. An order that no longer
   waits for a payment, or whose reservation ended at ``now`` (the sweep
   may be releasing it), opens nothing, and neither does one that made
   ``PAYMENT_MAX_ATTEMPTS`` attempts already;
3. the extension, the plan's compare-and-set, which the lock makes hit its
   row::

       UPDATE orders SET reserved_until = GREATEST(reserved_until, :now + :grace)
       WHERE id = :id AND status = 'pending_payment' AND reserved_until > :now
4. ``INSERT payments (attempt_no = n + 1, status = 'pending')`` for the
   order's total and currency, under ``uq_payments_order_id_pending``: one
   pending attempt per order, which the lock above already guarantees.

Once the transaction commits, the reservation outlasts the attempt's first
minutes whatever the sweep does: an order with a pending payment is never
expired by the first step of the sweep, and the provider's session, created
after this commit, is expired first by its later steps (expire-first).
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Row, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livecart_core.application.dto import (
    AttemptOpening,
    AttemptOpeningKind,
    PaymentProviderName,
    PaymentRecord,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.money import Money
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.tables import orders, payments
from livecart_core.infrastructure.db.units import transaction


def payment_record(row: Row[Any]) -> PaymentRecord:
    return PaymentRecord(
        id=row.id,
        order_id=row.order_id,
        attempt_no=row.attempt_no,
        provider=PaymentProviderName(row.provider),
        amount=Money(row.amount_minor, row.currency),
        return_url=row.return_url,
        status=PaymentStatus(row.status),
        created_at=row.created_at,
        provider_ref=row.provider_ref,
        redirect_url=row.redirect_url,
        provider_payment_ref=row.provider_payment_ref,
    )


class SqlPaymentRepository:
    """The payment attempts of the orders; see the module docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def open_attempt(
        self,
        order_id: UUID,
        *,
        provider: PaymentProviderName,
        return_url: str,
        now: datetime,
        grace: timedelta,
        max_attempts: int,
    ) -> AttemptOpening:
        if grace <= timedelta(0):
            raise ValueError(f"grace must be positive, not {grace}")
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be positive, not {max_attempts}")
        async with transaction(self._engine) as connection:
            order = (
                await connection.execute(
                    select(orders.c.status, orders.c.reserved_until)
                    .where(orders.c.id == order_id)
                    .with_for_update()
                )
            ).one_or_none()
            if order is None:
                raise RecordNotFound(f"no order {order_id}")
            attempts = (
                await connection.execute(select(payments).where(payments.c.order_id == order_id))
            ).all()
            pending = next(
                (row for row in attempts if row.status == PaymentStatus.PENDING.value), None
            )
            if pending is not None:
                return AttemptOpening(AttemptOpeningKind.PENDING, payment_record(pending))
            if order.status != OrderStatus.PENDING_PAYMENT.value or order.reserved_until <= now:
                return AttemptOpening(AttemptOpeningKind.NOT_PAYABLE)
            if len(attempts) >= max_attempts:
                return AttemptOpening(AttemptOpeningKind.EXHAUSTED)
            extended = await self._extend(connection, order_id, now=now, grace=grace)
            if extended is None:
                # Impossible while this transaction holds the order's row.
                raise RuntimeError(f"order {order_id} changed under its lock")
            payment = PaymentRecord(
                id=new_id(),
                order_id=order_id,
                attempt_no=max((row.attempt_no for row in attempts), default=0) + 1,
                provider=provider,
                amount=Money(extended.total_minor, extended.currency),
                return_url=return_url,
                status=PaymentStatus.PENDING,
                created_at=now,
            )
            await connection.execute(
                insert(payments).values(
                    id=payment.id,
                    order_id=order_id,
                    attempt_no=payment.attempt_no,
                    provider=payment.provider.value,
                    return_url=return_url,
                    amount_minor=payment.amount.amount_minor,
                    currency=payment.amount.currency,
                    status=payment.status.value,
                    created_at=now,
                )
            )
        return AttemptOpening(AttemptOpeningKind.OPENED, payment)

    async def record_checkout(
        self, payment_id: UUID, *, provider_ref: str, redirect_url: str
    ) -> PaymentRecord:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    update(payments)
                    .where(payments.c.id == payment_id, payments.c.provider_ref.is_(None))
                    .values(provider_ref=provider_ref, redirect_url=redirect_url)
                    .returning(*payments.c)
                )
            ).one_or_none()
            if row is None:
                # Recorded already, by an earlier call: the first session stays.
                row = (
                    await connection.execute(select(payments).where(payments.c.id == payment_id))
                ).one_or_none()
        if row is None:
            raise RecordNotFound(f"no payment {payment_id}")
        return payment_record(row)

    async def get_pending(self, order_id: UUID) -> PaymentRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(payments).where(
                        payments.c.order_id == order_id,
                        payments.c.status == PaymentStatus.PENDING.value,
                    )
                )
            ).one_or_none()
        return None if row is None else payment_record(row)

    async def latest(self, order_id: UUID) -> PaymentRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(payments)
                    .where(payments.c.order_id == order_id)
                    .order_by(payments.c.attempt_no.desc())
                    .limit(1)
                )
            ).one_or_none()
        return None if row is None else payment_record(row)

    @staticmethod
    async def _extend(
        connection: AsyncConnection, order_id: UUID, *, now: datetime, grace: timedelta
    ) -> Row[Any] | None:
        """The reservation extended to at least ``now + grace``, or ``None`` when it may not be."""
        until = literal(now + grace, orders.c.reserved_until.type)
        return (
            await connection.execute(
                update(orders)
                .where(
                    orders.c.id == order_id,
                    orders.c.status == OrderStatus.PENDING_PAYMENT.value,
                    orders.c.reserved_until > now,
                )
                .values(reserved_until=func.greatest(orders.c.reserved_until, until))
                .returning(orders.c.total_minor, orders.c.currency)
            )
        ).one_or_none()
