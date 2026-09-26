"""The SQL adapters of sessions and catalogs on PostgreSQL, beyond the scenarios of tests/contract.

``start`` is a compare-and-set that exactly one of several concurrent calls
applies; the writes to a catalog lock the session's row with ``FOR NO KEY
UPDATE``, which serializes them, so the rules that span rows hold under
concurrency, while a reservation of the same session, whose foreign keys
only take ``FOR KEY SHARE``, goes on.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import IntentSource, NewProduct, ReservationRequest
from livecart_core.application.ports import ProductConflict, ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.live_sessions import CatalogConflict
from livecart_core.domain.outcomes import IntentOutcome
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import LiveSessionStatus, TransitionOutcome
from livecart_core.infrastructure.db import tables
from livecart_core.testing.backends import PostgresBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

pytestmark = pytest.mark.integration

NOW = FAKE_CLOCK_START


@pytest.fixture
def backend(
    clean_database: None, app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> PostgresBackend:
    return PostgresBackend(app_engine, migrator_engine)


def _product(code: str, variant: str) -> NewProduct:
    return NewProduct(ProductCode(code), Variant(variant), "Robe", price_minor=100, stock_total=1)


async def test_one_of_several_concurrent_starts_applies(backend: PostgresBackend) -> None:
    session = await backend.add_live_session(status=LiveSessionStatus.DRAFT)

    results = await asyncio.gather(
        *(
            backend.live_sessions.set_status(
                session.seller_id,
                session.id,
                LiveSessionStatus.LIVE,
                now=NOW + timedelta(seconds=n),
            )
            for n in range(8)
        )
    )

    outcomes = [result.outcome for result in results]
    assert outcomes.count(TransitionOutcome.APPLIED) == 1
    assert outcomes.count(TransitionOutcome.NOOP) == 7
    # Every answer shows the one start that was applied.
    assert len({result.session.started_at for result in results}) == 1


async def test_concurrent_adds_that_would_mix_variants_leave_one_row(
    backend: PostgresBackend,
) -> None:
    for round_number in range(5):
        session = await backend.add_live_session(external_live_id=f"live-{round_number}")

        results = await asyncio.gather(
            backend.products.add(session.seller_id, session.id, _product("A12", ""), now=NOW),
            backend.products.add(session.seller_id, session.id, _product("A12", "M"), now=NOW),
            return_exceptions=True,
        )

        refused = [result for result in results if isinstance(result, ProductConflict)]
        assert len(refused) == 1
        assert refused[0].conflict is CatalogConflict.VARIANT_MIX
        assert len(await backend.products.list_for_session(session.id)) == 1


async def test_a_catalog_lock_leaves_the_reservations_of_the_session_running(
    backend: PostgresBackend, app_engine: AsyncEngine
) -> None:
    session = await backend.add_live_session()
    await backend.add_product(session.id, "B07", stock_total=3)
    comment_id = await backend.add_comment("comment-1")
    request = ReservationRequest(
        comment_id=comment_id,
        session_id=session.id,
        product_code=ProductCode("B07"),
        variant=Variant(),
        quantity=Quantity(1),
        source=IntentSource.PARSER,
        confidence=Decimal("1.00"),
        evidence=("B07",),
        now=NOW,
        reserved_until=NOW + timedelta(seconds=900),
    )

    # The lock a catalog write holds for its whole unit, held here on purpose.
    async with app_engine.begin() as holder:
        await holder.execute(
            select(tables.live_sessions.c.id)
            .where(tables.live_sessions.c.id == session.id)
            .with_for_update(key_share=True)
        )
        result = await asyncio.wait_for(backend.orders.reserve(request), timeout=5)
        # Another catalog write waits for the lock, until lock_timeout gives up on it.
        with pytest.raises(ServiceUnavailable):
            await backend.products.add(session.seller_id, session.id, _product("A12", ""), now=NOW)

    assert result is not None
    assert result.intent.outcome is IntentOutcome.RESERVED
    assert [str(row.code) for row in await backend.products.list_for_session(session.id)] == ["B07"]


async def test_another_sellers_id_writes_nothing(backend: PostgresBackend) -> None:
    session = await backend.add_live_session(status=LiveSessionStatus.DRAFT)
    other: UUID = await backend.add_seller()

    with pytest.raises(LookupError):
        await backend.live_sessions.set_status(other, session.id, LiveSessionStatus.LIVE, now=NOW)
    with pytest.raises(LookupError):
        await backend.products.add(other, session.id, _product("A12", ""), now=NOW)

    stored = await backend.live_sessions.get(session.id)
    assert stored == session
    assert await backend.products.list_for_session(session.id) == []
