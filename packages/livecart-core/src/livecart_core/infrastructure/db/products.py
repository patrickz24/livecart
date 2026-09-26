"""``ProductStockRepository`` on PostgreSQL: the products of the sessions and their stock counters.

The counters only move through conditional ``UPDATE`` statements whose
``WHERE`` holds the rule, so that the check and the write are one statement
and no read-then-write race exists: here the restock::

    UPDATE products SET stock_total = stock_total + :delta
    FROM live_sessions
    WHERE products.id = :id AND products.session_id = live_sessions.id
      AND live_sessions.id = :session AND live_sessions.seller_id = :seller
      AND live_sessions.status IN ('draft', 'live')
      AND stock_total + :delta >= reserved + sold
      AND stock_total + :delta <= :max
    RETURNING ...

and in ``SqlOrderRepository.reserve`` the reservation. When a reservation
holds the row, the restock waits and evaluates its ``WHERE`` again on the
counters the reservation committed. ``ck_products_stock`` is the last line of
defence behind both.

A seller's writes to a catalog (``add``, ``update``) first lock the
session's row with ``FOR NO KEY UPDATE``, filtered by the seller: the rules
that span several rows (a code has one row without a variant or only rows
with one, ``MAX_PRODUCTS_PER_SESSION``) are checked and written with no
other write to the catalog, and no change of the session's status, in
between. The reservations never lock the session's row, and the
``FOR KEY SHARE`` locks of their foreign keys do not conflict with
``NO KEY UPDATE``, so a live session keeps selling meanwhile.

The same goes for ``end``, which never waits for the reservations: one
whose statement started before the ``end`` committed still reads the
session as live, and may open a pending order in a session that is ended by
the time it commits. The stock stays exact (the product's row serializes the
reservations), and that order expires like any other (ADR 0005).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livecart_core.application.dto import (
    MAX_STOCK_TOTAL,
    NewProduct,
    Page,
    ProductChanges,
    ProductRecord,
    StockLevel,
    page_of,
)
from livecart_core.application.ports import ProductConflict, RecordNotFound, StockConflict
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.live_sessions import (
    MAX_PRODUCTS_PER_SESSION,
    CatalogConflict,
    check_session_edit,
    variant_conflict,
)
from livecart_core.domain.money import Money
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.live_sessions import owned_session
from livecart_core.infrastructure.db.tables import live_sessions, products
from livecart_core.infrastructure.db.units import transaction, violated_constraint

KEY_CONSTRAINT = "uq_products_session_id_code_variant"
# The statuses whose products take a restock: an ended session changes no more.
RESTOCKABLE = (LiveSessionStatus.DRAFT.value, LiveSessionStatus.LIVE.value)


def stock_level(row: Row[Any]) -> StockLevel:
    """The counters of a row that holds ``stock_total``, ``reserved`` and ``sold``."""
    return StockLevel(total=row.stock_total, reserved=row.reserved, sold=row.sold)


def product_record(row: Row[Any], *, currency: str) -> ProductRecord:
    return ProductRecord(
        id=row.id,
        session_id=row.session_id,
        code=ProductCode(row.code),
        variant=Variant(row.variant),
        name=row.name,
        price=Money(row.price_minor, currency),
        stock=stock_level(row),
    )


class SqlProductStockRepository:
    """Products and their stock, priced in the one currency of the deployment (DEFAULT_CURRENCY)."""

    def __init__(self, engine: AsyncEngine, *, currency: str) -> None:
        self._engine = engine
        self._currency = currency

    async def add(
        self, seller_id: UUID, session_id: UUID, product: NewProduct, *, now: datetime
    ) -> ProductRecord:
        try:
            async with transaction(self._engine) as connection:
                status = await self._lock_session(connection, seller_id, session_id)
                check_session_edit(status)
                await self._check_fits(connection, session_id, product.code, product.variant)
                size = await connection.scalar(
                    select(func.count()).where(products.c.session_id == session_id)
                )
                if (size or 0) >= MAX_PRODUCTS_PER_SESSION:
                    raise ProductConflict(CatalogConflict.FULL)
                row = (
                    await connection.execute(
                        insert(products)
                        .values(
                            id=new_id(),
                            session_id=session_id,
                            code=product.code.value,
                            variant=product.variant.value,
                            name=product.name,
                            price_minor=product.price_minor,
                            stock_total=product.stock_total,
                            created_at=now,
                        )
                        .returning(*products.c)
                    )
                ).one()
        except IntegrityError as error:
            # The lock keeps the key free between the check and the insert; the index still decides.
            if violated_constraint(error) == KEY_CONSTRAINT:
                raise ProductConflict(CatalogConflict.KEY_TAKEN) from error
            raise
        return self._record(row)

    async def get(self, product_id: UUID) -> ProductRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(select(products).where(products.c.id == product_id))
            ).one_or_none()
        return None if row is None else self._record(row)

    async def get_for_seller(
        self, seller_id: UUID, session_id: UUID, product_id: UUID
    ) -> ProductRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(products)
                    .join(live_sessions, products.c.session_id == live_sessions.c.id)
                    .where(products.c.id == product_id, owned_session(seller_id, session_id))
                )
            ).one_or_none()
        return None if row is None else self._record(row)

    async def list_for_session(self, session_id: UUID) -> list[ProductRecord]:
        async with transaction(self._engine) as connection:
            rows = (
                await connection.execute(
                    # UUID v7 ids sort by creation time.
                    select(products)
                    .where(products.c.session_id == session_id)
                    .order_by(products.c.id)
                )
            ).all()
        return self._records(rows)

    async def list_for_seller(
        self, seller_id: UUID, session_id: UUID, *, limit: int, cursor: UUID | None = None
    ) -> Page[ProductRecord]:
        query = (
            select(products)
            .join(live_sessions, products.c.session_id == live_sessions.c.id)
            .where(owned_session(seller_id, session_id))
        )
        if cursor is not None:
            query = query.where(products.c.id > cursor)
        async with transaction(self._engine) as connection:
            owned = await connection.scalar(
                select(live_sessions.c.id).where(owned_session(seller_id, session_id))
            )
            if owned is None:
                raise RecordNotFound(f"no live session {session_id} for this seller")
            rows = (await connection.execute(query.order_by(products.c.id).limit(limit + 1))).all()
        return page_of(self._records(rows), limit)

    async def update(
        self, seller_id: UUID, session_id: UUID, product_id: UUID, changes: ProductChanges
    ) -> ProductRecord:
        try:
            async with transaction(self._engine) as connection:
                status = await self._lock_session(connection, seller_id, session_id)
                current = (
                    await connection.execute(
                        select(products).where(
                            products.c.id == product_id, products.c.session_id == session_id
                        )
                    )
                ).one_or_none()
                if current is None:
                    raise RecordNotFound(f"no product {product_id} in session {session_id}")
                product = self._record(current)
                keys = changes.touches_keys(product)
                check_session_edit(status, keys=keys)
                code, variant = changes.key_of(product)
                if keys:
                    await self._check_fits(
                        connection, session_id, code, variant, excluding=product_id
                    )
                values: dict[str, object] = {"code": code.value, "variant": variant.value}
                if changes.name is not None:
                    values["name"] = changes.name
                if changes.price_minor is not None:
                    values["price_minor"] = changes.price_minor
                row = (
                    await connection.execute(
                        update(products)
                        .where(
                            products.c.id == product_id,
                            products.c.session_id == live_sessions.c.id,
                            owned_session(seller_id, session_id),
                        )
                        .values(**values)
                        .returning(*products.c)
                    )
                ).one()
        except IntegrityError as error:
            if violated_constraint(error) == KEY_CONSTRAINT:
                raise ProductConflict(CatalogConflict.KEY_TAKEN) from error
            raise
        return self._record(row)

    async def restock(
        self, seller_id: UUID, session_id: UUID, product_id: UUID, delta: int
    ) -> ProductRecord:
        total = products.c.stock_total + delta
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    update(products)
                    .where(
                        products.c.id == product_id,
                        products.c.session_id == live_sessions.c.id,
                        owned_session(seller_id, session_id),
                        live_sessions.c.status.in_(RESTOCKABLE),
                        total >= products.c.reserved + products.c.sold,
                        total <= MAX_STOCK_TOTAL,
                    )
                    .values(stock_total=total)
                    .returning(*products.c)
                )
            ).one_or_none()
            if row is not None:
                return self._record(row)
            # Nothing matched: read why, with the same filter.
            current = (
                await connection.execute(
                    select(products, live_sessions.c.status.label("session_status"))
                    .join(live_sessions, products.c.session_id == live_sessions.c.id)
                    .where(products.c.id == product_id, owned_session(seller_id, session_id))
                )
            ).one_or_none()
        if current is None:
            raise RecordNotFound(f"no product {product_id} in session {session_id}")
        check_session_edit(LiveSessionStatus(current.session_status))
        new_total = current.stock_total + delta
        if new_total > MAX_STOCK_TOTAL:
            raise StockConflict(
                f"product {product_id}: a total of {new_total} is above {MAX_STOCK_TOTAL}",
                above_maximum=True,
            )
        raise StockConflict(
            f"product {product_id}: a total of {new_total} is below the "
            f"{current.reserved} reserved and {current.sold} sold"
        )

    # --- Catalog rules under the session's lock ----------------------------------------

    @staticmethod
    async def _lock_session(
        connection: AsyncConnection, seller_id: UUID, session_id: UUID
    ) -> LiveSessionStatus:
        """Lock the seller's session row for the unit and return its status."""
        status = await connection.scalar(
            select(live_sessions.c.status)
            .where(owned_session(seller_id, session_id))
            .with_for_update(key_share=True)
        )
        if status is None:
            raise RecordNotFound(f"no live session {session_id} for this seller")
        return LiveSessionStatus(status)

    @staticmethod
    async def _check_fits(
        connection: AsyncConnection,
        session_id: UUID,
        code: ProductCode,
        variant: Variant,
        *,
        excluding: UUID | None = None,
    ) -> None:
        """``ProductConflict`` unless the catalog can hold ``code`` and ``variant``."""
        query = select(products.c.variant).where(
            products.c.session_id == session_id, products.c.code == code.value
        )
        if excluding is not None:
            query = query.where(products.c.id != excluding)
        same_code = [Variant(value) for value in (await connection.scalars(query)).all()]
        conflict = variant_conflict(same_code, variant)
        if conflict is not None:
            raise ProductConflict(conflict)

    def _record(self, row: Row[Any]) -> ProductRecord:
        return product_record(row, currency=self._currency)

    def _records(self, rows: Sequence[Row[Any]]) -> list[ProductRecord]:
        return [self._record(row) for row in rows]
