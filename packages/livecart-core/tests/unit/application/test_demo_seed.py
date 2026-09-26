"""``SeedDemo`` over the in-memory fakes: what the first run creates, and what the next ones keep.

tests/integration/test_demo_seed.py runs the seed command on PostgreSQL.
"""

import pytest

from livecart_core.application.demo_seed import (
    DEMO_SELLERS,
    FRIDAY_DROP,
    LOADTEST_LIVE_ID,
    SeedConflict,
    SeedDemo,
    Seeded,
    SeedReport,
)
from livecart_core.application.dto import NewLiveSession, NewProduct, Platform
from livecart_core.application.seller_auth import AuthenticateSeller
from livecart_core.application.storefront import ViewStorefront
from livecart_core.domain.catalog import Catalog
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.testing.builders import catalog_product, demo_catalog
from livecart_core.testing.fakes import FakeClock, FakePasswordHasher, InMemoryDatabase

PASSWORD = "demo password of the tests"
SEEDED_TABLES = ("sellers", "sessions", "products")


def _seed(database: InMemoryDatabase, clock: FakeClock | None = None) -> SeedDemo:
    return SeedDemo(
        database.sellers,
        database.live_sessions,
        database.products,
        FakePasswordHasher(),
        clock if clock is not None else FakeClock(),
    )


def _stock(database: InMemoryDatabase, live_id: str) -> dict[tuple[str, str], int]:
    session = next(s for s in database.tables.sessions.values() if s.external_live_id == live_id)
    return {
        (product.code.value, product.variant.value): product.stock.total
        for product in database.tables.products.values()
        if product.session_id == session.id
    }


async def test_the_first_run_creates_both_sellers_and_starts_both_sessions() -> None:
    database = InMemoryDatabase()

    report = await _seed(database)(password=PASSWORD)

    assert [(seller.seller.slug, seller.seeded) for seller in report.sellers] == [
        ("demo-seller", Seeded.CREATED),
        ("sample-shop", Seeded.CREATED),
    ]
    demo, sample = report.sellers
    assert [
        (s.session.external_live_id, s.session.platform, s.session.status, s.products_added)
        for s in demo.sessions
    ] == [
        ("loadtest", Platform.SIMULATOR, LiveSessionStatus.LIVE, 1),
        ("friday-drop", Platform.SIMULATOR, LiveSessionStatus.LIVE, 15),
    ]
    assert all(s.session.seller_id == demo.seller.id for s in demo.sessions)
    assert sample.sessions == ()
    assert report.session(LOADTEST_LIVE_ID).title == "Load test"


async def test_friday_drop_sells_the_catalog_of_the_parser_corpus_and_one_product_more() -> None:
    database = InMemoryDatabase()

    report = await _seed(database)(password=PASSWORD)

    seeded = await database.live_sessions.load_catalog(report.session("friday-drop").id)
    # The corpus's expectations hold on the seeded session: no corpus comment names F8.
    assert seeded == Catalog((*demo_catalog(), catalog_product("F8", "Foulard en soie")))
    assert len(seeded) == 7
    assert _stock(database, "friday-drop")[("A12", "M")] == 5


async def test_the_demo_storefront_shows_friday_drop_the_session_started_last() -> None:
    database = InMemoryDatabase()
    await _seed(database)(password=PASSWORD)
    view = ViewStorefront(database.sellers, database.live_sessions, database.products)

    demo = await view("demo-seller")
    sample = await view("sample-shop")

    assert demo is not None
    assert demo.session is not None
    assert demo.session.external_live_id == "friday-drop"
    assert len({product.code for product in demo.products}) == 7
    assert sample is not None
    assert sample.session is None


async def test_the_load_test_session_sells_z99_alone_with_200_in_stock() -> None:
    database = InMemoryDatabase()

    await _seed(database)(password=PASSWORD)

    assert _stock(database, "loadtest") == {("Z99", ""): 200}


async def test_both_sellers_sign_in_with_the_demo_password() -> None:
    database, passwords, clock = InMemoryDatabase(), FakePasswordHasher(), FakeClock()
    await SeedDemo(database.sellers, database.live_sessions, database.products, passwords, clock)(
        password=PASSWORD
    )
    authenticate = AuthenticateSeller(database.sellers, database.refresh_tokens, passwords, clock)

    for seller in DEMO_SELLERS:
        grant = await authenticate(seller.email, PASSWORD)
        assert grant.refresh_token is not None


async def test_a_second_run_finds_everything_and_changes_nothing() -> None:
    database, clock = InMemoryDatabase(), FakeClock()
    await _seed(database, clock)(password=PASSWORD)
    # Rows are frozen records, replaced whole on change: copied dicts are a snapshot.
    snapshot = {name: dict(getattr(database.tables, name)) for name in SEEDED_TABLES}
    clock.advance(3600)

    report = await _seed(database, clock)(password="another password entirely")

    assert {seller.seeded for seller in report.sellers} == {Seeded.FOUND}
    assert {
        (session.seeded, session.products_added)
        for seller in report.sellers
        for session in seller.sessions
    } == {(Seeded.FOUND, 0)}
    # Neither the passwords nor the sessions' start times moved.
    assert {name: dict(getattr(database.tables, name)) for name in SEEDED_TABLES} == snapshot


async def test_a_run_completes_a_partial_demo_and_starts_its_draft() -> None:
    database, clock = InMemoryDatabase(), FakeClock()
    seller = database.add_seller(slug="demo-seller", display_name="Renamed by hand")
    draft = await database.live_sessions.add(
        seller.id,
        NewLiveSession("My own title", Platform.SIMULATOR, "friday-drop"),
        now=clock.now(),
    )
    await database.products.add(
        seller.id,
        draft.id,
        NewProduct(ProductCode("B07"), Variant(), "Sac", price_minor=1, stock_total=1),
        now=clock.now(),
    )

    report = await _seed(database, clock)(password=PASSWORD)

    demo = report.sellers[0]
    friday = demo.sessions[1]
    assert (demo.seeded, demo.seller.display_name) == (Seeded.FOUND, "Renamed by hand")
    assert (friday.seeded, friday.products_added) == (Seeded.FOUND, len(FRIDAY_DROP.products) - 1)
    assert (friday.session.status, friday.session.title) == (LiveSessionStatus.LIVE, "My own title")
    # The product that was there keeps its stock.
    assert _stock(database, "friday-drop")[("B07", "")] == 1


async def test_a_demo_live_id_held_by_another_seller_is_a_conflict() -> None:
    database, clock = InMemoryDatabase(), FakeClock()
    squatter = database.add_seller(slug="squatter")
    database.add_live_session(seller_id=squatter.id, external_live_id="friday-drop")

    with pytest.raises(SeedConflict, match="simulator/friday-drop belongs to another seller"):
        await _seed(database, clock)(password=PASSWORD)


async def test_an_ended_demo_session_is_reported_as_it_is() -> None:
    database, clock = InMemoryDatabase(), FakeClock()
    report = await _seed(database, clock)(password=PASSWORD)
    seller_id = report.sellers[0].seller.id
    loadtest = report.session("loadtest")
    await database.live_sessions.set_status(
        seller_id, loadtest.id, LiveSessionStatus.ENDED, now=clock.now()
    )
    # A product missing from it could no longer be added: the run must not try.
    database.tables.products = {
        product_id: product
        for product_id, product in database.tables.products.items()
        if product.session_id != loadtest.id
    }

    again = await _seed(database, clock)(password=PASSWORD)

    ended = again.sellers[0].sessions[0]
    assert (ended.session.status, ended.seeded, ended.products_added) == (
        LiveSessionStatus.ENDED,
        Seeded.FOUND,
        0,
    )
    assert _stock(database, "loadtest") == {}


async def test_a_password_the_domain_refuses_creates_nothing() -> None:
    database = InMemoryDatabase()

    with pytest.raises(ValueError, match="12 to 128 characters"):
        await _seed(database)(password="short")

    assert database.tables.sellers == {}


async def test_a_report_names_only_the_sessions_it_seeded() -> None:
    report = await _seed(InMemoryDatabase())(password=PASSWORD)

    with pytest.raises(KeyError):
        report.session("another-live")
    with pytest.raises(KeyError):
        SeedReport(()).session("friday-drop")
