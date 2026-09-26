"""A family revoked while a rotation of one of its tokens commits: no successor survives.

Under READ COMMITTED, an ``UPDATE`` that waits for a row lock re-reads that
row once the lock is released, but never sees rows inserted after the
statement started. A revocation that waits for a rotation in flight would
then revoke the used token and miss its successor. These tests hold a
rotation open on one connection, start the revocation on another, and
release the rotation only once the revocation waits for its lock.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livecart_core.application.dto import RefreshTokenRecord
from livecart_core.application.seller_auth import hash_refresh_token, new_refresh_token
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.refresh_tokens import SqlRefreshTokenRepository
from livecart_core.infrastructure.db.sellers import SqlSellerRepository
from livecart_core.infrastructure.db.tables import refresh_tokens
from livecart_core.testing.backends import LOCKED_ACCOUNT
from livecart_core.testing.fakes import FAKE_CLOCK_START

pytestmark = pytest.mark.integration

NOW = FAKE_CLOCK_START
LATER = NOW + timedelta(seconds=5)
# A live view, unlike pg_stat_activity, which a transaction reads from a snapshot.
PG_LOCKS = sa.table("pg_locks", sa.column("granted", sa.Boolean))


async def _issue(app_engine: AsyncEngine) -> RefreshTokenRecord:
    seller = await SqlSellerRepository(app_engine).add(
        email="seller@example.test",
        slug="demo-seller",
        display_name="Demo",
        password_hash=LOCKED_ACCOUNT,
        now=NOW,
    )
    return await SqlRefreshTokenRepository(app_engine).issue(
        seller.id,
        hash_refresh_token(new_refresh_token()),
        now=NOW,
        expires_at=NOW + timedelta(days=30),
    )


@asynccontextmanager
async def _rotation_in_flight(
    app_engine: AsyncEngine, token: RefreshTokenRecord
) -> AsyncIterator[tuple[AsyncConnection, UUID]]:
    """The statements of ``rotate``, run but not committed: the token's row stays locked."""
    async with app_engine.connect() as connection, connection.begin():
        successor_id = new_id()
        await connection.execute(
            update(refresh_tokens).where(refresh_tokens.c.id == token.id).values(used_at=NOW)
        )
        await connection.execute(
            insert(refresh_tokens).values(
                id=successor_id,
                seller_id=token.seller_id,
                family_id=token.family_id,
                token_hash=hash_refresh_token(new_refresh_token()),
                expires_at=token.expires_at,
                created_at=NOW,
            )
        )
        await connection.execute(
            update(refresh_tokens)
            .where(refresh_tokens.c.id == token.id)
            .values(replaced_by_id=successor_id)
        )
        yield connection, successor_id


async def _wait_for_a_lock_waiter(connection: AsyncConnection) -> None:
    for _ in range(500):
        waiting = await connection.scalar(
            select(func.count()).select_from(PG_LOCKS).where(PG_LOCKS.c.granted.is_(False))
        )
        if waiting:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the revocation never waited for the rotation's lock")


async def _revoked_at(app_engine: AsyncEngine, token_id: UUID) -> object:
    async with app_engine.connect() as connection:
        return await connection.scalar(
            select(refresh_tokens.c.revoked_at).where(refresh_tokens.c.id == token_id)
        )


async def test_a_revocation_waiting_on_a_rotation_also_revokes_the_successor(
    clean_database: None, app_engine: AsyncEngine
) -> None:
    token = await _issue(app_engine)
    repository = SqlRefreshTokenRepository(app_engine)

    async with _rotation_in_flight(app_engine, token) as (connection, successor_id):
        revocation = asyncio.create_task(repository.revoke_family(token.family_id, now=LATER))
        await _wait_for_a_lock_waiter(connection)
    # The rotation has committed; the revocation goes on.

    assert await revocation == 2
    assert await _revoked_at(app_engine, token.id) == LATER
    assert await _revoked_at(app_engine, successor_id) == LATER


async def test_one_statement_alone_would_leave_the_successor_active(
    clean_database: None, app_engine: AsyncEngine
) -> None:
    """Why ``revoke_family`` repeats its UPDATE: the premise, shown on PostgreSQL itself."""
    token = await _issue(app_engine)

    async def revoke_once() -> int:
        async with app_engine.begin() as connection:
            result = await connection.execute(
                update(refresh_tokens)
                .where(
                    refresh_tokens.c.family_id == token.family_id,
                    refresh_tokens.c.revoked_at.is_(None),
                )
                .values(revoked_at=LATER)
            )
            return result.rowcount

    async with _rotation_in_flight(app_engine, token) as (connection, successor_id):
        revocation = asyncio.create_task(revoke_once())
        await _wait_for_a_lock_waiter(connection)

    assert await revocation == 1
    assert await _revoked_at(app_engine, successor_id) is None
