"""``SellerRepository`` on PostgreSQL: seller accounts, unique by e-mail whatever its case.

``uq_sellers_email`` is a unique index on ``lower(email)``: two accounts can
never differ by the case of an e-mail, and ``find_by_email`` compares
``lower(email) = lower(:email)``, the very expression of the index, so the
lookup is an index scan. The e-mails are ASCII (``domain.sellers``), where
PostgreSQL's ``lower()`` and Python's agree.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import SellerRecord
from livecart_core.application.ports import SellerConflict
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.tables import sellers
from livecart_core.infrastructure.db.units import transaction, violated_constraint

EMAIL_INDEX = "uq_sellers_email"
SLUG_CONSTRAINT = "uq_sellers_slug"


def seller_record(row: Row[Any]) -> SellerRecord:
    return SellerRecord(
        id=row.id,
        email=row.email,
        slug=row.slug,
        display_name=row.display_name,
        password_hash=row.password_hash,
        created_at=row.created_at,
    )


class SqlSellerRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(
        self, *, email: str, slug: str, display_name: str, password_hash: str, now: datetime
    ) -> SellerRecord:
        try:
            async with transaction(self._engine) as connection:
                row = (
                    await connection.execute(
                        insert(sellers)
                        .values(
                            id=new_id(),
                            email=email,
                            slug=slug,
                            display_name=display_name,
                            password_hash=password_hash,
                            created_at=now,
                        )
                        .returning(*sellers.c)
                    )
                ).one()
        except IntegrityError as error:
            # The index or constraint says which field another seller has.
            constraint = violated_constraint(error)
            if constraint == EMAIL_INDEX:
                raise SellerConflict("email") from error
            if constraint == SLUG_CONSTRAINT:
                raise SellerConflict("slug") from error
            raise
        return seller_record(row)

    async def get(self, seller_id: UUID) -> SellerRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(select(sellers).where(sellers.c.id == seller_id))
            ).one_or_none()
        return None if row is None else seller_record(row)

    async def find_by_email(self, email: str) -> SellerRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(sellers).where(func.lower(sellers.c.email) == func.lower(email))
                )
            ).one_or_none()
        return None if row is None else seller_record(row)
