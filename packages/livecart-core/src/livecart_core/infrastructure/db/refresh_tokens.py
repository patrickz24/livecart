"""``RefreshTokenRepository`` on PostgreSQL: atomic rotation, and family revocation without a gap.

``rotate`` is one READ COMMITTED transaction of SQLAlchemy Core constructs:

1. the conditional update that uses the token::

       UPDATE refresh_tokens SET used_at = :now
       WHERE token_hash = :h AND used_at IS NULL AND revoked_at IS NULL
         AND expires_at > :now
       RETURNING ...

   When two refreshes send the same token, the second ``UPDATE`` waits for
   the first one's row lock, then evaluates its ``WHERE`` again on the row the
   first committed: ``used_at`` is set, no row comes back, and nothing is
   written. Exactly one rotation wins, without a retry;
2. ``INSERT`` of the successor, in the same family and with the same expiry;
3. ``UPDATE`` of the used token's ``replaced_by_id``, once the row it points
   to exists (the foreign key is checked at each statement).

``revoke_family`` repeats ``UPDATE ... SET revoked_at = :now WHERE family_id =
:f AND revoked_at IS NULL`` until it revokes nothing more. One statement is
not enough under READ COMMITTED: if a rotation holds the lock of a token of
the family, the ``UPDATE`` waits for it, then revokes that token, but the
successor the rotation inserted is not in the snapshot the statement started
from, and would stay active. The next statement takes a new snapshot, which
holds it. The last statement finds nothing, and the rows revoked stay locked
until the commit, so no rotation can start from them in between.

Every time comes from the caller's ``now`` (the ``Clock`` port), never from
the database clock.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import RefreshTokenRecord
from livecart_core.application.ports import RecordNotFound
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.tables import refresh_tokens
from livecart_core.infrastructure.db.units import transaction, violated_constraint

SELLER_FOREIGN_KEY = "fk_refresh_tokens_seller_id_sellers"


def refresh_token_record(row: Row[Any]) -> RefreshTokenRecord:
    return RefreshTokenRecord(
        id=row.id,
        seller_id=row.seller_id,
        family_id=row.family_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
        used_at=row.used_at,
        revoked_at=row.revoked_at,
        replaced_by_id=row.replaced_by_id,
    )


class SqlRefreshTokenRepository:
    """Refresh tokens by hash; see the module docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def issue(
        self, seller_id: UUID, token_hash: bytes, *, now: datetime, expires_at: datetime
    ) -> RefreshTokenRecord:
        try:
            async with transaction(self._engine) as connection:
                row = (
                    await connection.execute(
                        insert(refresh_tokens)
                        .values(
                            id=new_id(),
                            seller_id=seller_id,
                            family_id=new_id(),
                            token_hash=token_hash,
                            expires_at=expires_at,
                            created_at=now,
                        )
                        .returning(*refresh_tokens.c)
                    )
                ).one()
        except IntegrityError as error:
            if violated_constraint(error) == SELLER_FOREIGN_KEY:
                raise RecordNotFound(f"no seller {seller_id}") from error
            raise
        return refresh_token_record(row)

    async def rotate(
        self, token_hash: bytes, successor_hash: bytes, *, now: datetime
    ) -> RefreshTokenRecord | None:
        async with transaction(self._engine) as connection:
            used = (
                await connection.execute(
                    update(refresh_tokens)
                    .where(
                        refresh_tokens.c.token_hash == token_hash,
                        refresh_tokens.c.used_at.is_(None),
                        refresh_tokens.c.revoked_at.is_(None),
                        refresh_tokens.c.expires_at > now,
                    )
                    .values(used_at=now)
                    .returning(
                        refresh_tokens.c.id,
                        refresh_tokens.c.seller_id,
                        refresh_tokens.c.family_id,
                        refresh_tokens.c.expires_at,
                    )
                )
            ).one_or_none()
            if used is None:
                return None
            successor = (
                await connection.execute(
                    insert(refresh_tokens)
                    .values(
                        id=new_id(),
                        seller_id=used.seller_id,
                        family_id=used.family_id,
                        token_hash=successor_hash,
                        expires_at=used.expires_at,
                        created_at=now,
                    )
                    .returning(*refresh_tokens.c)
                )
            ).one()
            await connection.execute(
                update(refresh_tokens)
                .where(refresh_tokens.c.id == used.id)
                .values(replaced_by_id=successor.id)
            )
        return refresh_token_record(successor)

    async def find(self, token_hash: bytes) -> RefreshTokenRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(refresh_tokens).where(refresh_tokens.c.token_hash == token_hash)
                )
            ).one_or_none()
        return None if row is None else refresh_token_record(row)

    async def get(self, token_id: UUID) -> RefreshTokenRecord | None:
        async with transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    select(refresh_tokens).where(refresh_tokens.c.id == token_id)
                )
            ).one_or_none()
        return None if row is None else refresh_token_record(row)

    async def revoke_family(self, family_id: UUID, *, now: datetime) -> int:
        revoke = (
            update(refresh_tokens)
            .where(refresh_tokens.c.family_id == family_id, refresh_tokens.c.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        revoked = 0
        async with transaction(self._engine) as connection:
            # Until a statement revokes nothing: see the module docstring.
            while count := (await connection.execute(revoke)).rowcount:
                revoked += count
        return revoked
