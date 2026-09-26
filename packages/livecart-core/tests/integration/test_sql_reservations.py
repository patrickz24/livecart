"""What the SQL adapter of unit 3 does on PostgreSQL beyond the shared scenarios of tests/contract.

The reservation is one conditional ``UPDATE`` and no row is read to decide
it; a unit runs in READ COMMITTED under the server-side timeouts; a lock held
too long comes out as ``ServiceUnavailable`` with nothing written.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import IntentSource, ReservationRequest
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import CommentStatus
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.units import transaction
from livecart_core.order_tokens import OrderTokens
from livecart_core.testing.backends import ORDER_TOKENS, PostgresBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

pytestmark = pytest.mark.integration

NOW = FAKE_CLOCK_START


def _request(comment_id: UUID, session_id: UUID) -> ReservationRequest:
    return ReservationRequest(
        comment_id=comment_id,
        session_id=session_id,
        product_code=ProductCode("B07"),
        variant=Variant(),
        quantity=Quantity(1),
        source=IntentSource.PARSER,
        confidence=Decimal("1.00"),
        evidence=("je prends", "B07"),
        now=NOW,
        reserved_until=NOW + timedelta(seconds=900),
    )


@contextmanager
def _statements(engine: AsyncEngine) -> Iterator[list[str]]:
    """Every SQL statement ``engine`` sends while the block runs."""
    sent: list[str] = []

    def record(*args: Any) -> None:
        sent.append(args[2])  # (connection, cursor, statement, parameters, context, executemany)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


@pytest.fixture
async def seeded(
    clean_database: None, app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> tuple[PostgresBackend, UUID, UUID]:
    """The backend, a live session selling B07, and a received comment."""
    backend = PostgresBackend(app_engine, migrator_engine)
    session = await backend.add_live_session()
    await backend.add_product(session.id, "B07", stock_total=3)
    return backend, session.id, await backend.add_comment("comment-1")


async def test_the_stock_is_reserved_by_one_conditional_update(
    seeded: tuple[PostgresBackend, UUID, UUID], app_engine: AsyncEngine
) -> None:
    backend, session_id, comment_id = seeded

    with _statements(app_engine) as sent:
        result = await backend.orders.reserve(_request(comment_id, session_id))

    assert result is not None
    assert result.order is not None
    updates = [statement for statement in sent if statement.startswith("UPDATE products")]
    assert len(updates) == 1
    reservation = " ".join(updates[0].split())
    assert "SET reserved=(products.reserved + $1::INTEGER)" in reservation
    assert "FROM live_sessions" in reservation
    assert "products.session_id = live_sessions.id" in reservation
    assert "live_sessions.status = $" in reservation
    assert "products.available >= $" in reservation
    assert "RETURNING" in reservation
    # Nothing is locked ahead of the write, and the per-buyer rule is left to its index.
    assert not any("FOR UPDATE" in statement for statement in sent)
    assert not any(statement.startswith("SELECT orders") for statement in sent)
    # The reservation's savepoint, and inside it the savepoint of the reference draw.
    assert [statement.split()[0] for statement in sent if "SAVEPOINT" in statement] == [
        "SAVEPOINT",
        "SAVEPOINT",
        "RELEASE",
        "RELEASE",
    ]


async def test_every_unit_runs_in_read_committed_under_the_server_timeouts(
    database_schema: None, app_engine: AsyncEngine
) -> None:
    async with transaction(app_engine) as connection:
        settings = [
            (await connection.execute(text(f"SHOW {name}"))).scalar_one()
            for name in ("transaction_isolation", "statement_timeout", "lock_timeout")
        ]
        role = (await connection.execute(text("SELECT current_user"))).scalar_one()

    assert settings == ["read committed", "5s", "2s"]
    assert role == "livecart_app"


async def test_a_lock_held_too_long_is_a_transient_failure_with_nothing_written(
    seeded: tuple[PostgresBackend, UUID, UUID], migrator_engine: AsyncEngine
) -> None:
    backend, session_id, comment_id = seeded

    async with migrator_engine.begin() as holder:
        # Another transaction holds the product's row, longer than lock_timeout.
        await holder.execute(
            select(tables.products.c.id).where(tables.products.c.code == "B07").with_for_update()
        )
        with pytest.raises(ServiceUnavailable, match="database unavailable"):
            await backend.orders.reserve(_request(comment_id, session_id))

    assert await backend.comment_state(comment_id) == (CommentStatus.RECEIVED, None)
    assert await backend.count("purchase_intents") == 0
    assert await backend.orders.find_reservation(comment_id) is None


async def test_an_order_stores_the_hash_of_its_token_and_never_the_token(
    seeded: tuple[PostgresBackend, UUID, UUID], app_engine: AsyncEngine
) -> None:
    backend, session_id, comment_id = seeded

    result = await backend.orders.reserve(_request(comment_id, session_id))

    assert result is not None
    assert result.order is not None
    async with app_engine.connect() as connection:
        row = (
            await connection.execute(
                select(tables.orders).where(tables.orders.c.id == result.order.id)
            )
        ).one()
    token = ORDER_TOKENS.token_for(result.order.id)
    assert OrderTokens.verify(token, row.access_token_hash)
    assert not OrderTokens.verify(ORDER_TOKENS.token_for(comment_id), row.access_token_hash)
    assert token.encode() not in b"".join(
        value if isinstance(value, bytes) else str(value).encode() for value in row
    )
    # UUID v7: the application generated the ids, in creation order.
    assert result.order.id.version == 7
    assert result.intent.id.version == 7
    assert result.intent.id < result.order.id
