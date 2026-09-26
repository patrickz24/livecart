"""Put a live session back to its seeded state: the demo seed's ``--reset-loadtest``.

The load test replays its comment identifiers from one run to the next, and
the inbox would take every replay for a duplicate: the session's comments go,
with the intents, orders, payments and shipments they led to, and its
products' counters go back to zero with their seeded stock. One transaction,
so that a failed reset leaves the session as it was.

Only ``livecart_migrator`` may run it: ``livecart_app`` holds no DELETE
privilege, which is why the seed connects as the migrator.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.units import transaction


@dataclass(frozen=True, slots=True)
class SessionReset:
    """How many rows of each kind the reset deleted."""

    comments: int
    intents: int
    orders: int


async def reset_live_session(
    engine: AsyncEngine,
    session_id: UUID,
    stock_totals: Mapping[tuple[ProductCode, Variant], int],
) -> SessionReset:
    """Delete what the session's comments led to, then reset its products' counters.

    ``stock_totals`` gives the seeded stock of each product by code and
    variant; a product it does not name keeps its total.
    """
    orders = tables.orders
    order_ids = select(orders.c.id).where(orders.c.session_id == session_id)
    products = tables.products
    async with transaction(engine) as connection:
        # Children first: every foreign key points up this list.
        for child in (tables.shipments, tables.payments):
            await connection.execute(delete(child).where(child.c.order_id.in_(order_ids)))
        deleted_orders = await connection.execute(
            delete(orders).where(orders.c.session_id == session_id)
        )
        intents = tables.purchase_intents
        deleted_intents = await connection.execute(
            delete(intents).where(intents.c.session_id == session_id)
        )
        comments = tables.comments
        deleted_comments = await connection.execute(
            delete(comments).where(comments.c.session_id == session_id)
        )
        await connection.execute(
            update(products).where(products.c.session_id == session_id).values(reserved=0, sold=0)
        )
        for (code, variant), total in stock_totals.items():
            await connection.execute(
                update(products)
                .where(
                    products.c.session_id == session_id,
                    products.c.code == code.value,
                    products.c.variant == variant.value,
                )
                .values(stock_total=total)
            )
    return SessionReset(
        comments=deleted_comments.rowcount,
        intents=deleted_intents.rowcount,
        orders=deleted_orders.rowcount,
    )
