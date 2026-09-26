from uuid import UUID

import pytest

from livecart_core.application.dto import Platform, StockLevel
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.catalog import Catalog
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.money import Money
from livecart_core.domain.state_machines import LiveSessionStatus, TransitionOutcome
from livecart_core.testing.builders import demo_catalog
from livecart_core.testing.fakes import FAKE_CLOCK_START, InMemoryDatabase

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


async def test_the_catalog_reads_back_as_it_was_seeded() -> None:
    database = InMemoryDatabase()
    session = database.add_live_session()
    rows = database.add_catalog(session.id, demo_catalog(), price=Money(2_000, "EUR"))

    catalog = await database.live_sessions.load_catalog(session.id)

    assert catalog == demo_catalog()
    assert len(rows) == 3 + 1 + 4 + 4 + 1 + 1
    assert {row.price for row in rows} == {Money(2_000, "EUR")}


async def test_a_catalog_holds_only_its_session_products() -> None:
    database = InMemoryDatabase()
    session = database.add_live_session()
    other = database.add_live_session(external_live_id="live-2")
    database.add_product(other.id, "Z99")

    assert await database.live_sessions.load_catalog(session.id) == Catalog(())
    with pytest.raises(RecordNotFound):
        await database.live_sessions.load_catalog(UNKNOWN)


async def test_a_session_is_found_by_its_platform_live_id() -> None:
    database = InMemoryDatabase()
    session = database.add_live_session(external_live_id="live-9", platform=Platform.INSTAGRAM)
    sessions = database.live_sessions

    assert await sessions.find_by_live_id(Platform.INSTAGRAM, "live-9") == session
    assert await sessions.find_by_live_id(Platform.TIKTOK, "live-9") is None
    assert await sessions.get(session.id) == session
    assert await sessions.get(UNKNOWN) is None


async def test_a_session_starts_then_ends_with_its_timestamps() -> None:
    database = InMemoryDatabase()
    session = database.add_live_session(status=LiveSessionStatus.DRAFT)
    sessions = database.live_sessions
    later = NOW.replace(hour=13)

    started = await sessions.set_status(session.id, LiveSessionStatus.LIVE, now=NOW)
    again = await sessions.set_status(session.id, LiveSessionStatus.LIVE, now=later)
    ended = await sessions.set_status(session.id, LiveSessionStatus.ENDED, now=later)

    row = await sessions.get(session.id)
    assert (started, again, ended) == (
        TransitionOutcome.APPLIED,
        TransitionOutcome.NOOP,
        TransitionOutcome.APPLIED,
    )
    assert row is not None
    assert (row.status, row.started_at, row.ended_at) == (LiveSessionStatus.ENDED, NOW, later)


async def test_an_ended_session_does_not_go_live_again() -> None:
    database = InMemoryDatabase()
    session = database.add_live_session(status=LiveSessionStatus.ENDED)

    with pytest.raises(InvalidTransition):
        await database.live_sessions.set_status(session.id, LiveSessionStatus.LIVE, now=NOW)
    with pytest.raises(RecordNotFound):
        await database.live_sessions.set_status(UNKNOWN, LiveSessionStatus.LIVE, now=NOW)
    with pytest.raises(ValueError, match="naive"):
        await database.live_sessions.set_status(
            session.id, LiveSessionStatus.ENDED, now=NOW.replace(tzinfo=None)
        )
