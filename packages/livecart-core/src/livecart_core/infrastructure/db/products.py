"""``ProductStockRepository`` on PostgreSQL: the products of the sessions and their stock counters.

The counters only move through conditional ``UPDATE`` statements whose
``WHERE`` holds the rule, so that the check and the write are one statement
and no read-then-write race exists: here the restock, and in
``SqlOrderRepository.reserve`` the reservation. ``ck_products_stock`` is the
last line of defence behind both.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Row, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import ProductRecord, StockLevel
from livecart_core.application.ports import RecordNotFound, StockConflict
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.infrastructure.db.tables import products
from livecart_core.infrastructure.db.units import transaction


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

    async def get(self, product_id: UUID) -> ProductRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(select(products).where(products.c.id == product_id))
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

    async def restock(self, product_id: UUID, delta: int) -> ProductRecord:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    update(products)
                    .where(
                        products.c.id == product_id,
                        products.c.stock_total + delta >= products.c.reserved + products.c.sold,
                    )
                    .values(stock_total=products.c.stock_total + delta)
                    .returning(*products.c)
                )
            ).one_or_none()
            if row is not None:
                return self._record(row)
            current = (
                await connection.execute(select(products).where(products.c.id == product_id))
            ).one_or_none()
        if current is None:
            raise RecordNotFound(f"no product {product_id}")
        raise StockConflict(
            f"product {product_id}: a total of {current.stock_total + delta} is below the "
            f"{current.reserved} reserved and {current.sold} sold"
        )

    def _record(self, row: Row[Any]) -> ProductRecord:
        return product_record(row, currency=self._currency)

    def _records(self, rows: Sequence[Row[Any]]) -> list[ProductRecord]:
        return [self._record(row) for row in rows]
