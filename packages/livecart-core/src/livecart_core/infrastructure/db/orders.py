"""``OrderReservations`` on PostgreSQL: unit 3 of a comment in one READ COMMITTED transaction.

``reserve`` runs, in this order, SQLAlchemy Core constructs only:

1. a plain read of the session's seller and of the product, which locks nothing;
2. ``UPDATE comments SET status = 'processed' ... WHERE id = :id AND
   status = 'received'``: no row means another delivery settled the comment,
   and the unit ends having written nothing;
3. ``SAVEPOINT``, then the reservation itself, a single conditional update::

       UPDATE products SET reserved = reserved + :q
       FROM live_sessions
       WHERE products.id = :id AND products.session_id = live_sessions.id
         AND live_sessions.status = 'live' AND products.available >= :q
       RETURNING ...

   No row: the intent is ``out_of_stock``. When another transaction holds the
   product's row, this ``UPDATE`` waits, then PostgreSQL evaluates its
   ``WHERE`` again on the row version that transaction committed: the last
   unit goes to one of them and the other finds nothing left. No read races
   the write, so nothing is ever oversold and nothing needs a retry;
4. ``INSERT purchase_intents`` (``reserved``), then ``INSERT orders``. If the
   buyer already holds a pending order for the product, the partial unique
   index ``uq_orders_pending_buyer_product`` refuses the order: the unit goes
   back to the savepoint, which undoes the reservation and the intent, and
   records the intent as ``already_reserved`` instead;
5. the order's reference is drawn at random, so two orders may draw the
   same one: each ``INSERT orders`` runs under a nested savepoint, and when
   ``uq_orders_reference`` refuses it, only that savepoint is rolled back
   and a new reference is drawn, the reservation kept. After
   ``REFERENCE_DRAWS`` (3) collisions the unit raises ``ServiceUnavailable``
   and the whole transaction rolls back: the comment stays received and its
   redelivery tries again. No ``IntegrityError`` ever reaches the caller.

Locks are taken in one order, the comment then the product, so two units
cannot deadlock. Every deadline comes from the request's ``now`` (the
``Clock`` port), never from the database clock.
"""

import secrets
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy import Row, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livecart_core.application.dto import (
    BuyerOrderRecord,
    IntentSource,
    OrderRecord,
    PurchaseIntentRecord,
    ReservationRequest,
    ReservationResult,
    ShippingAddress,
    StockLevel,
)
from livecart_core.application.ports import REFERENCE_DRAWS, RecordNotFound, ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.outcomes import IntentOutcome
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.reference import (
    OrderReference,
    RandomSource,
    generate_order_reference,
)
from livecart_core.domain.state_machines import CommentStatus, LiveSessionStatus, OrderStatus
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.products import stock_level
from livecart_core.infrastructure.db.tables import (
    comments,
    live_sessions,
    orders,
    products,
    purchase_intents,
)
from livecart_core.infrastructure.db.units import transaction, violated_constraint

PENDING_ORDER_INDEX = "uq_orders_pending_buyer_product"
REFERENCE_INDEX = "uq_orders_reference"


def intent_record(row: Row[Any]) -> PurchaseIntentRecord:
    return PurchaseIntentRecord(
        id=row.id,
        comment_id=row.comment_id,
        session_id=row.session_id,
        product_id=row.product_id,
        quantity=Quantity(row.quantity),
        source=IntentSource(row.source),
        confidence=row.confidence,
        evidence=tuple(row.evidence),
        outcome=IntentOutcome(row.outcome),
    )


def order_record(row: Row[Any]) -> OrderRecord:
    return OrderRecord(
        id=row.id,
        reference=OrderReference(row.reference),
        seller_id=row.seller_id,
        session_id=row.session_id,
        intent_id=row.intent_id,
        product_id=row.product_id,
        quantity=Quantity(row.quantity),
        unit_price=Money(row.unit_price_minor, row.currency),
        total=Money(row.total_minor, row.currency),
        status=OrderStatus(row.status),
        reserved_until=row.reserved_until,
        buyer_platform_user_id=row.buyer_platform_user_id,
        created_at=row.created_at,
        buyer_notified_at=row.buyer_notified_at,
        shipping_address=(
            None
            if row.shipping_address is None
            else ShippingAddress.model_validate(row.shipping_address)
        ),
        paid_at=row.paid_at,
        expired_at=row.expired_at,
    )


class _PendingOrderExists(Exception):
    """The buyer already holds a pending order for the product (the partial unique index)."""


class SqlOrderRepository:
    """Unit 3 and the reads of what it recorded; see the module docstring.

    ``access_token_hash`` gives the ``orders.access_token_hash`` of a new
    order from its id: the hash of the token that ``livecart_core.order_tokens``
    derives, so that no token is ever stored. ``random_source`` draws the
    order references; tests pass a scripted one to force a collision.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        currency: str,
        access_token_hash: Callable[[UUID], bytes],
        random_source: RandomSource = secrets,
    ) -> None:
        self._engine = engine
        self._currency = currency
        self._access_token_hash = access_token_hash
        self._random_source = random_source

    async def reserve(self, request: ReservationRequest) -> ReservationResult | None:
        async with transaction(self._engine) as connection:
            seller_id, product_id = await self._target(connection, request)
            buyer = await self._settle_comment(connection, request)
            if buyer is None:
                return None
            return await self._reserve(connection, request, seller_id, product_id, buyer)

    async def find_reservation(self, comment_id: UUID) -> ReservationResult | None:
        async with transaction(self._engine) as connection:
            intent_row = (
                await connection.execute(
                    select(purchase_intents).where(purchase_intents.c.comment_id == comment_id)
                )
            ).one_or_none()
            if intent_row is None:
                return None
            intent = intent_record(intent_row)
            order_row = (
                await connection.execute(select(orders).where(orders.c.intent_id == intent.id))
            ).one_or_none()
            stock = await self._stock(connection, intent.product_id)
        order = None if order_row is None else order_record(order_row)
        return ReservationResult(intent, order, stock)

    async def get(self, order_id: UUID) -> OrderRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(select(orders).where(orders.c.id == order_id))
            ).one_or_none()
        return None if row is None else order_record(row)

    async def get_by_reference(self, reference: OrderReference) -> OrderRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(orders).where(orders.c.reference == reference.value)
                )
            ).one_or_none()
        return None if row is None else order_record(row)

    async def find_by_reference(self, reference: OrderReference) -> BuyerOrderRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(
                        orders,
                        products.c.code.label("product_code"),
                        products.c.variant.label("product_variant"),
                        products.c.name.label("product_name"),
                    )
                    .join(products, orders.c.product_id == products.c.id)
                    .where(orders.c.reference == reference.value)
                )
            ).one_or_none()
        if row is None:
            return None
        return BuyerOrderRecord(
            order=order_record(row),
            product_code=ProductCode(row.product_code),
            product_variant=Variant(row.product_variant),
            product_name=row.product_name,
            access_token_hash=row.access_token_hash,
        )

    # --- The steps of unit 3 -----------------------------------------------------------

    async def _target(
        self, connection: AsyncConnection, request: ReservationRequest
    ) -> tuple[UUID, UUID]:
        """The session's seller and the product's id; a plain read, which locks nothing."""
        row = (
            await connection.execute(
                select(live_sessions.c.seller_id, products.c.id)
                .select_from(
                    live_sessions.outerjoin(
                        products,
                        (products.c.session_id == live_sessions.c.id)
                        & (products.c.code == request.product_code.value)
                        & (products.c.variant == request.variant.value),
                    )
                )
                .where(live_sessions.c.id == request.session_id)
            )
        ).one_or_none()
        if row is None:
            raise RecordNotFound(f"no live session {request.session_id}")
        if row.id is None:
            raise RecordNotFound(
                f"no product {request.product_code} {request.variant.value!r} "
                f"in session {request.session_id}"
            )
        return row.seller_id, row.id

    async def _settle_comment(
        self, connection: AsyncConnection, request: ReservationRequest
    ) -> str | None:
        """Settle a received comment as processed; its author, or ``None`` when it was settled."""
        author: str | None = (
            await connection.execute(
                update(comments)
                .where(
                    comments.c.id == request.comment_id,
                    comments.c.status == CommentStatus.RECEIVED.value,
                )
                .values(status=CommentStatus.PROCESSED.value, session_id=request.session_id)
                .returning(comments.c.author_platform_user_id)
            )
        ).scalar_one_or_none()
        if author is None and (
            await connection.scalar(
                select(comments.c.id).where(comments.c.id == request.comment_id)
            )
            is None
        ):
            raise RecordNotFound(f"no comment {request.comment_id}")
        return author

    async def _reserve(
        self,
        connection: AsyncConnection,
        request: ReservationRequest,
        seller_id: UUID,
        product_id: UUID,
        buyer: str,
    ) -> ReservationResult:
        quantity = request.quantity.value
        reservation = await connection.begin_nested()
        reserved = (
            await connection.execute(
                update(products)
                .where(
                    products.c.id == product_id,
                    products.c.session_id == live_sessions.c.id,
                    live_sessions.c.status == LiveSessionStatus.LIVE.value,
                    products.c.available >= quantity,
                )
                .values(reserved=products.c.reserved + quantity)
                .returning(
                    products.c.price_minor,
                    products.c.stock_total,
                    products.c.reserved,
                    products.c.sold,
                )
            )
        ).one_or_none()
        if reserved is None:
            await reservation.commit()
            stock = await self._stock(connection, product_id)
            intent = await self._record_intent(
                connection, request, product_id, IntentOutcome.OUT_OF_STOCK
            )
            return ReservationResult(intent, None, stock)

        stock = stock_level(reserved)
        intent = await self._record_intent(connection, request, product_id, IntentOutcome.RESERVED)
        try:
            order = await self._open_order(
                connection,
                request,
                intent,
                seller_id=seller_id,
                buyer=buyer,
                unit_price=Money(reserved.price_minor, self._currency),
            )
        except _PendingOrderExists:
            # Back to before the reservation: the stock and the intent are undone.
            await reservation.rollback()
            intent = await self._record_intent(
                connection, request, product_id, IntentOutcome.ALREADY_RESERVED
            )
            return ReservationResult(
                intent, None, replace(stock, reserved=stock.reserved - quantity)
            )
        await reservation.commit()
        return ReservationResult(intent, order, stock)

    async def _record_intent(
        self,
        connection: AsyncConnection,
        request: ReservationRequest,
        product_id: UUID,
        outcome: IntentOutcome,
    ) -> PurchaseIntentRecord:
        intent = PurchaseIntentRecord(
            id=new_id(),
            comment_id=request.comment_id,
            session_id=request.session_id,
            product_id=product_id,
            quantity=request.quantity,
            source=request.source,
            confidence=request.confidence,
            evidence=request.evidence,
            outcome=outcome,
        )
        await connection.execute(
            insert(purchase_intents).values(
                id=intent.id,
                comment_id=intent.comment_id,
                session_id=intent.session_id,
                product_id=intent.product_id,
                quantity=intent.quantity.value,
                source=intent.source.value,
                confidence=intent.confidence,
                evidence=list(intent.evidence),
                outcome=intent.outcome.value,
                created_at=request.now,
            )
        )
        return intent

    async def _open_order(
        self,
        connection: AsyncConnection,
        request: ReservationRequest,
        intent: PurchaseIntentRecord,
        *,
        seller_id: UUID,
        buyer: str,
        unit_price: Money,
    ) -> OrderRecord:
        """Insert the pending order, drawing its reference again after a collision.

        Each draw runs under a savepoint of its own, nested in the
        reservation's: a colliding reference undoes that insert only, not
        the stock reserved before it.
        """
        order_id = new_id()
        for _ in range(REFERENCE_DRAWS):
            order = OrderRecord(
                id=order_id,
                reference=generate_order_reference(self._random_source),
                seller_id=seller_id,
                session_id=request.session_id,
                intent_id=intent.id,
                product_id=intent.product_id,
                quantity=request.quantity,
                unit_price=unit_price,
                total=unit_price * request.quantity.value,
                status=OrderStatus.PENDING_PAYMENT,
                reserved_until=request.reserved_until,
                buyer_platform_user_id=buyer,
                created_at=request.now,
            )
            draw = await connection.begin_nested()
            try:
                await connection.execute(insert(orders).values(self._order_values(order)))
            except IntegrityError as error:
                await draw.rollback()
                constraint = violated_constraint(error)
                if constraint == REFERENCE_INDEX:
                    continue
                if constraint == PENDING_ORDER_INDEX:
                    raise _PendingOrderExists from error
                raise
            await draw.commit()
            return order
        raise ServiceUnavailable(f"{REFERENCE_DRAWS} order reference draws collided")

    def _order_values(self, order: OrderRecord) -> dict[str, Any]:
        return {
            "id": order.id,
            "reference": order.reference.value,
            "access_token_hash": self._access_token_hash(order.id),
            "seller_id": order.seller_id,
            "session_id": order.session_id,
            "intent_id": order.intent_id,
            "product_id": order.product_id,
            "quantity": order.quantity.value,
            "unit_price_minor": order.unit_price.amount_minor,
            "total_minor": order.total.amount_minor,
            "currency": order.total.currency,
            "status": order.status.value,
            "reserved_until": order.reserved_until,
            "buyer_platform_user_id": order.buyer_platform_user_id,
            "created_at": order.created_at,
        }

    @staticmethod
    async def _stock(connection: AsyncConnection, product_id: UUID) -> StockLevel:
        row = (
            await connection.execute(
                select(products.c.stock_total, products.c.reserved, products.c.sold).where(
                    products.c.id == product_id
                )
            )
        ).one()
        return stock_level(row)
