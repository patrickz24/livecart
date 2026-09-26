"""``SellerLiveSessions`` and ``SellerProducts`` over the fakes: the clock, the seller, the pages.

The repository rules themselves are the scenarios of tests/contract; these
tests pin what the use cases add: the time comes from the ``Clock`` port, a
missing or foreign row is ``RecordNotFound``, and a page size is bounded.
"""

from datetime import timedelta
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    MAX_PAGE_SIZE,
    MAX_STOCK_TOTAL,
    LiveSessionChanges,
    NewLiveSession,
    NewProduct,
    Platform,
    ProductChanges,
    StockLevel,
)
from livecart_core.application.live_sessions import SellerLiveSessions
from livecart_core.application.ports import RecordNotFound
from livecart_core.application.products import SellerProducts
from livecart_core.domain.codes import NO_VARIANT, ProductCode
from livecart_core.domain.money import Money
from livecart_core.domain.state_machines import LiveSessionStatus, TransitionOutcome
from livecart_core.testing.fakes import (
    FAKE_CLOCK_START,
    FakeClock,
    FakeEventPublisher,
    InMemoryDatabase,
)

UNKNOWN = UUID(int=404)


def _use_cases() -> tuple[InMemoryDatabase, FakeClock, SellerLiveSessions, SellerProducts]:
    database, clock = InMemoryDatabase(), FakeClock()
    return (
        database,
        clock,
        SellerLiveSessions(database.live_sessions, clock, FakeEventPublisher()),
        SellerProducts(database.products, clock),
    )


async def test_a_session_is_created_started_and_ended_at_the_clocks_time() -> None:
    database, clock, sessions, _ = _use_cases()
    seller = database.add_seller()

    created = await sessions.create(
        seller.id, NewLiveSession("Friday drop", Platform.TIKTOK, "tt-1")
    )
    started = await sessions.start(seller.id, created.id)
    clock.advance(timedelta(hours=1))
    ended = await sessions.end(seller.id, created.id)

    assert (created.status, created.created_at) == (LiveSessionStatus.DRAFT, FAKE_CLOCK_START)
    assert (started.outcome, started.session.started_at) == (
        TransitionOutcome.APPLIED,
        FAKE_CLOCK_START,
    )
    assert (ended.outcome, ended.session.ended_at) == (
        TransitionOutcome.APPLIED,
        FAKE_CLOCK_START + timedelta(hours=1),
    )
    assert await sessions.get(seller.id, created.id) == ended.session


async def test_a_seller_reads_edits_and_lists_only_their_sessions() -> None:
    database, _, sessions, _ = _use_cases()
    seller, other = database.add_seller(), database.add_seller()
    created = await sessions.create(seller.id, NewLiveSession("Drop", Platform.TIKTOK, "tt-1"))

    renamed = await sessions.update(seller.id, created.id, LiveSessionChanges(title="Big drop"))
    page = await sessions.list(seller.id, limit=MAX_PAGE_SIZE)

    assert renamed.title == "Big drop"
    assert page.items == (renamed,)
    assert (await sessions.list(other.id, limit=1)).items == ()
    with pytest.raises(RecordNotFound):
        await sessions.get(other.id, created.id)
    with pytest.raises(RecordNotFound):
        await sessions.get(seller.id, UNKNOWN)


@pytest.mark.parametrize("limit", [0, MAX_PAGE_SIZE + 1])
async def test_a_page_size_is_bounded_before_any_query(limit: int) -> None:
    database, _, sessions, products = _use_cases()
    session = database.add_live_session(seller_id=database.add_seller().id)

    with pytest.raises(ValueError, match="page holds"):
        await sessions.list(session.seller_id, limit=limit)
    with pytest.raises(ValueError, match="page holds"):
        await products.list(session.seller_id, session.id, limit=limit)


async def test_a_restock_moves_the_stock_by_a_bounded_delta() -> None:
    database, _, _, products = _use_cases()
    session = database.add_live_session(seller_id=database.add_seller().id)
    product = database.add_product(session.id, "Z99", stock_total=10)

    restocked = await products.restock(session.seller_id, session.id, product.id, 5)

    assert restocked.stock == StockLevel(15)
    for delta in (0, MAX_STOCK_TOTAL + 1, -MAX_STOCK_TOTAL - 1):
        with pytest.raises(ValueError, match="restock moves"):
            await products.restock(session.seller_id, session.id, product.id, delta)


async def test_a_seller_adds_reads_edits_and_lists_the_products_of_a_session() -> None:
    database, _, _, products = _use_cases()
    seller, other = database.add_seller(), database.add_seller()
    session = database.add_live_session(seller_id=seller.id, status=LiveSessionStatus.DRAFT)
    new = NewProduct(ProductCode("A12"), NO_VARIANT, "Robe", price_minor=2_490, stock_total=3)

    added = await products.add(seller.id, session.id, new)
    repriced = await products.update(
        seller.id, session.id, added.id, ProductChanges(price_minor=1_990)
    )
    page = await products.list(seller.id, session.id, limit=10)

    assert (added.price, added.stock) == (Money(2_490, "EUR"), StockLevel(3))
    assert repriced.price == Money(1_990, "EUR")
    assert page.items == (repriced,)
    assert await products.get(seller.id, session.id, added.id) == repriced
    with pytest.raises(RecordNotFound):
        await products.get(other.id, session.id, added.id)
    with pytest.raises(RecordNotFound):
        await products.get(seller.id, session.id, UNKNOWN)
