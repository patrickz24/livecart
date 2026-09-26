"""``LiveSessionActivity`` on PostgreSQL: a session's snapshot, read in one consistent view.

``snapshot`` runs five reads in one REPEATABLE READ, read-only transaction,
so that all of them see the database as it was at the first: an order and
its product's counters agree, even while reservations commit meanwhile.
READ COMMITTED would give each statement its own view, and a snapshot could
show an order whose stock it does not count yet. Each list reads the
session's rows by their ``session_id`` index, newest first (UUID v7 ids sort
by creation), at most ``limit`` of each:

- the session, then its products in creation order;
- the purchase intents, each with the id of the order it opened (``LEFT
  JOIN orders ON orders.intent_id``, unique by ``uq_orders_intent_id``);
- the rejected comments and their reasons (the partial index of the rejected
  comments by session);
- the orders.

SQLAlchemy Core constructs only, as everywhere in the adapters.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import RecordedIntent, RejectedComment, SessionActivity
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.outcomes import RejectionReason
from livecart_core.domain.state_machines import CommentStatus
from livecart_core.infrastructure.db.live_sessions import live_session_record
from livecart_core.infrastructure.db.orders import intent_record, order_record
from livecart_core.infrastructure.db.products import product_record
from livecart_core.infrastructure.db.tables import (
    comments,
    live_sessions,
    orders,
    products,
    purchase_intents,
)
from livecart_core.infrastructure.db.units import transaction


class SqlLiveSessionActivity:
    """A session's snapshot, priced in the deployment's currency; see the module docstring."""

    def __init__(self, engine: AsyncEngine, *, currency: str) -> None:
        # The same pool, with every transaction of this adapter consistent and read-only.
        self._engine = engine.execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True
        )
        self._currency = currency

    async def snapshot(self, session_id: UUID, *, limit: int) -> SessionActivity:
        async with transaction(self._engine) as connection:
            session = (
                await connection.execute(
                    select(live_sessions).where(live_sessions.c.id == session_id)
                )
            ).one_or_none()
            if session is None:
                raise RecordNotFound(f"no live session {session_id}")
            product_rows = (
                await connection.execute(
                    select(products)
                    .where(products.c.session_id == session_id)
                    .order_by(products.c.id)
                )
            ).all()
            intent_rows = (
                await connection.execute(
                    select(purchase_intents, orders.c.id.label("order_id"))
                    .select_from(
                        purchase_intents.outerjoin(
                            orders, orders.c.intent_id == purchase_intents.c.id
                        )
                    )
                    .where(purchase_intents.c.session_id == session_id)
                    .order_by(purchase_intents.c.id.desc())
                    .limit(limit)
                )
            ).all()
            rejection_rows = (
                await connection.execute(
                    select(comments.c.id, comments.c.rejection_reason)
                    .where(
                        comments.c.session_id == session_id,
                        comments.c.status == CommentStatus.REJECTED.value,
                    )
                    .order_by(comments.c.id.desc())
                    .limit(limit)
                )
            ).all()
            order_rows = (
                await connection.execute(
                    select(orders)
                    .where(orders.c.session_id == session_id)
                    .order_by(orders.c.id.desc())
                    .limit(limit)
                )
            ).all()
        return SessionActivity(
            session=live_session_record(session),
            products=tuple(product_record(row, currency=self._currency) for row in product_rows),
            intents=tuple(RecordedIntent(intent_record(row), row.order_id) for row in intent_rows),
            rejections=tuple(
                RejectedComment(row.id, RejectionReason(row.rejection_reason))
                for row in rejection_rows
            ),
            orders=tuple(order_record(row) for row in order_rows),
        )
