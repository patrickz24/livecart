from uuid import UUID

import pytest

from livecart_core.application.dto import NewLiveSession, NewProduct, Platform, StockLevel
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.testing.builders import demo_catalog
from livecart_core.testing.fakes import FAKE_CLOCK_START, LOCKED_ACCOUNT, InMemoryDatabase

NOW = FAKE_CLOCK_START
UNKNOWN = UUID(int=404)


# --- Seeding ---------------------------------------------------------------------------


def test_ids_are_sequential() -> None:
    database = InMemoryDatabase()

    assert [database.new_id() for _ in range(3)] == [UUID(int=1), UUID(int=2), UUID(int=3)]


def test_a_seeded_session_is_live_on_the_simulator_by_default() -> None:
    database = InMemoryDatabase()

    session = database.add_live_session()

    assert session.status is LiveSessionStatus.LIVE
    assert (session.platform, session.external_live_id) == (Platform.SIMULATOR, "live-1")
    assert session.seller_id != session.id
    assert database.tables.sessions[session.id] == session


def test_a_live_id_is_unique_per_platform() -> None:
    database = InMemoryDatabase()
    database.add_live_session()

    database.add_live_session(platform=Platform.TIKTOK)
    with pytest.raises(ValueError, match="taken"):
        database.add_live_session()


def test_a_product_is_unique_per_code_and_variant_in_its_session() -> None:
    database = InMemoryDatabase()
    session = database.add_live_session()
    database.add_product(session.id, "A12", variant="M")

    database.add_product(session.id, "A12", variant="L")
    other = database.add_live_session(external_live_id="live-2")
    database.add_product(other.id, "A12", variant="M")
    with pytest.raises(ValueError, match="already in the session"):
        database.add_product(session.id, "A12", variant="M")


@pytest.mark.parametrize(("first", "second"), [("", "M"), ("M", "")])
def test_a_code_does_not_mix_rows_with_and_without_variant(first: str, second: str) -> None:
    database = InMemoryDatabase()
    session = database.add_live_session()
    database.add_product(session.id, "A12", variant=first)

    with pytest.raises(ValueError, match="cannot mix"):
        database.add_product(session.id, "A12", variant=second)


def test_a_product_needs_a_known_session() -> None:
    with pytest.raises(RecordNotFound):
        InMemoryDatabase().add_product(UNKNOWN, "A12")


def test_a_seeded_product_has_a_default_price_and_stock() -> None:
    database = InMemoryDatabase()
    session = database.add_live_session()

    product = database.add_product(session.id, "B07", name="Sac en cuir", stock_total=3)

    assert product.code == ProductCode("B07")
    assert product.variant == Variant()
    assert product.price == Money(1_000, "EUR")
    assert product.stock == StockLevel(3)
    assert database.find_product(session.id, ProductCode("B07"), Variant()) == product
    assert database.find_product(session.id, ProductCode("B07"), Variant("M")) is None


# --- Live sessions ---------------------------------------------------------------------
# The scenarios every backend shares are in tests/contract/test_live_sessions.py.


async def test_the_catalog_helper_seeds_one_row_per_code_and_variant() -> None:
    database = InMemoryDatabase()
    session = database.add_live_session()
    rows = database.add_catalog(session.id, demo_catalog(), price=Money(2_000, "EUR"))

    catalog = await database.live_sessions.load_catalog(session.id)

    assert catalog == demo_catalog()
    assert len(rows) == 3 + 1 + 4 + 4 + 1 + 1
    assert {row.price for row in rows} == {Money(2_000, "EUR")}


def test_a_seeded_seller_has_a_locked_account() -> None:
    database = InMemoryDatabase()

    seller = database.add_seller(slug="demo-seller", display_name="Demo")

    assert (seller.slug, seller.display_name, seller.email) == (
        "demo-seller",
        "Demo",
        "demo-seller@example.test",
    )
    assert seller.password_hash == LOCKED_ACCOUNT
    assert database.tables.sellers[seller.id] == seller
    assert database.add_seller().slug == f"seller-{database.new_id().int - 1}"


async def test_every_time_a_session_repository_takes_is_aware() -> None:
    database = InMemoryDatabase()
    seller = database.add_seller()
    session = database.add_live_session(seller_id=seller.id, status=LiveSessionStatus.DRAFT)
    naive = NOW.replace(tzinfo=None)
    new = NewLiveSession(title="Drop", platform=Platform.TIKTOK, external_live_id="tt-1")

    with pytest.raises(ValueError, match="naive"):
        await database.live_sessions.add(seller.id, new, now=naive)
    with pytest.raises(ValueError, match="naive"):
        await database.live_sessions.set_status(
            seller.id, session.id, LiveSessionStatus.LIVE, now=naive
        )
    with pytest.raises(ValueError, match="naive"):
        await database.products.add(
            seller.id,
            session.id,
            NewProduct(ProductCode("A12"), Variant(), "Robe", price_minor=100, stock_total=1),
            now=naive,
        )
