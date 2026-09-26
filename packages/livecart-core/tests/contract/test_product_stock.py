"""``ProductStockRepository`` on every backend: the in-memory fake, then PostgreSQL.

A seller reaches only the products of their own sessions, and a session's
status decides what its catalog still takes (``domain.live_sessions``).
"""

from uuid import UUID

import pytest

from livecart_core.application.dto import (
    MAX_STOCK_TOTAL,
    LiveSessionRecord,
    NewProduct,
    ProductChanges,
    StockLevel,
)
from livecart_core.application.ports import ProductConflict, RecordNotFound, StockConflict
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.errors import EditRefused
from livecart_core.domain.live_sessions import MAX_PRODUCTS_PER_SESSION, CatalogConflict
from livecart_core.domain.money import Money
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.testing.backends import ReservationBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
UNKNOWN = UUID(int=404)


async def test_a_product_reads_back_as_it_was_seeded(backend: ReservationBackend) -> None:
    session = await backend.add_live_session()

    seeded = await backend.add_product(
        session.id, "C3", variant="Bleu nuit", name="T-shirt coton", price=Money(1_990, "EUR")
    )

    product = await backend.products.get(seeded.id)
    assert product == seeded
    assert product is not None
    assert (product.code, product.variant) == (ProductCode("C3"), Variant("Bleu nuit"))
    assert product.stock == StockLevel(10)


async def test_products_are_listed_per_session_in_creation_order(
    backend: ReservationBackend,
) -> None:
    session = await backend.add_live_session()
    other = await backend.add_live_session(external_live_id="live-2")
    second = await backend.add_product(session.id, "C3", variant="Rouge")
    await backend.add_product(other.id, "C3", variant="Rouge")
    first = await backend.add_product(session.id, "A12", variant="M")

    assert await backend.products.list_for_session(session.id) == [second, first]
    assert await backend.products.list_for_session(UNKNOWN) == []
    assert await backend.products.get(first.id) == first
    assert await backend.products.get(UNKNOWN) is None


async def test_a_restock_adds_or_removes_units(backend: ReservationBackend) -> None:
    session = await backend.add_live_session()
    product = await backend.add_product(session.id, "Z99", stock_total=10)
    restock = backend.products.restock

    added = await restock(session.seller_id, session.id, product.id, 5)
    removed = await restock(session.seller_id, session.id, product.id, -12)

    assert added.stock == StockLevel(15)
    assert removed.stock == StockLevel(3)
    assert await backend.products.get(product.id) == removed


async def test_a_restock_never_goes_below_what_is_reserved_and_sold(
    backend: ReservationBackend,
) -> None:
    session = await backend.add_live_session()
    product = await backend.add_product(session.id, "Z99", stock_total=10)
    # Reservations and sales go through the orders; setting them directly keeps this test short.
    await backend.set_stock(product.id, StockLevel(10, reserved=3, sold=2))
    restock = backend.products.restock

    assert (await restock(session.seller_id, session.id, product.id, -5)).stock.available == 0
    with pytest.raises(StockConflict, match="below") as refused:
        await restock(session.seller_id, session.id, product.id, -1)

    assert not refused.value.above_maximum
    stored = await backend.products.get(product.id)
    assert stored is not None
    assert stored.stock == StockLevel(5, reserved=3, sold=2)


async def test_a_restock_never_goes_above_the_maximum(backend: ReservationBackend) -> None:
    session = await backend.add_live_session()
    product = await backend.add_product(session.id, "Z99", stock_total=10)
    restock = backend.products.restock

    full = await restock(session.seller_id, session.id, product.id, MAX_STOCK_TOTAL - 10)
    with pytest.raises(StockConflict, match="above") as refused:
        await restock(session.seller_id, session.id, product.id, 1)

    assert full.stock == StockLevel(MAX_STOCK_TOTAL)
    assert refused.value.above_maximum
    assert await backend.products.get(product.id) == full


async def test_a_restock_reaches_only_the_sellers_products_of_a_session_not_ended(
    backend: ReservationBackend,
) -> None:
    session = await backend.add_live_session(status=LiveSessionStatus.DRAFT)
    product = await backend.add_product(session.id, "Z99", stock_total=10)
    sibling = await backend.add_live_session(external_live_id="live-2", seller_id=session.seller_id)
    other = await backend.add_seller()
    restock = backend.products.restock

    drafted = await restock(session.seller_id, session.id, product.id, 1)
    with pytest.raises(RecordNotFound):
        await restock(other, session.id, product.id, 1)
    with pytest.raises(RecordNotFound):
        await restock(session.seller_id, sibling.id, product.id, 1)
    with pytest.raises(RecordNotFound):
        await restock(session.seller_id, session.id, UNKNOWN, 1)
    await backend.set_session_status(session.id, LiveSessionStatus.ENDED)
    with pytest.raises(EditRefused):
        await restock(session.seller_id, session.id, product.id, 1)

    assert drafted.stock == StockLevel(11)
    assert await backend.products.get(product.id) == drafted


# --- A seller's catalog ----------------------------------------------------------------


def _new(code: str = "A12", variant: str = "", *, stock_total: int = 5) -> NewProduct:
    return NewProduct(
        ProductCode(code),
        Variant(variant),
        "Robe fleurie",
        price_minor=2_490,
        stock_total=stock_total,
    )


async def _session(
    backend: ReservationBackend, status: LiveSessionStatus = LiveSessionStatus.DRAFT
) -> LiveSessionRecord:
    return await backend.add_live_session(status=status)


async def test_a_seller_adds_a_product_to_their_session(backend: ReservationBackend) -> None:
    session = await _session(backend)

    product = await backend.products.add(
        session.seller_id, session.id, _new("C3", "Bleu nuit", stock_total=7), now=NOW
    )

    assert (product.session_id, product.code, product.variant, product.name) == (
        session.id,
        ProductCode("C3"),
        Variant("Bleu nuit"),
        "Robe fleurie",
    )
    assert (product.price, product.stock) == (Money(2_490, "EUR"), StockLevel(7))
    assert await backend.products.get(product.id) == product
    assert await backend.products.list_for_session(session.id) == [product]
    found = await backend.products.get_for_seller(session.seller_id, session.id, product.id)
    assert found == product


async def test_a_live_session_takes_new_products_and_an_ended_one_does_not(
    backend: ReservationBackend,
) -> None:
    live = await _session(backend, LiveSessionStatus.LIVE)
    ended = await backend.add_live_session(
        external_live_id="live-2", status=LiveSessionStatus.ENDED, seller_id=live.seller_id
    )

    await backend.products.add(live.seller_id, live.id, _new(), now=NOW)
    with pytest.raises(EditRefused):
        await backend.products.add(ended.seller_id, ended.id, _new(), now=NOW)

    assert await backend.products.list_for_session(ended.id) == []


@pytest.mark.parametrize(
    ("first", "second", "conflict"),
    [
        (("A12", "M"), ("A12", "M"), CatalogConflict.KEY_TAKEN),
        (("A12", ""), ("A12", ""), CatalogConflict.KEY_TAKEN),
        (("A12", ""), ("A12", "M"), CatalogConflict.VARIANT_MIX),
        (("A12", "M"), ("A12", ""), CatalogConflict.VARIANT_MIX),
    ],
)
async def test_a_catalog_refuses_a_taken_key_and_a_variant_mix(
    backend: ReservationBackend,
    first: tuple[str, str],
    second: tuple[str, str],
    conflict: CatalogConflict,
) -> None:
    session = await _session(backend)
    kept = await backend.products.add(session.seller_id, session.id, _new(*first), now=NOW)

    with pytest.raises(ProductConflict) as refused:
        await backend.products.add(session.seller_id, session.id, _new(*second), now=NOW)

    assert refused.value.conflict is conflict
    assert await backend.products.list_for_session(session.id) == [kept]


async def test_a_code_takes_several_variants_and_another_session_the_same_key(
    backend: ReservationBackend,
) -> None:
    session = await _session(backend)
    other = await backend.add_live_session(external_live_id="live-2")

    for variant in ("S", "M", "L"):
        await backend.products.add(session.seller_id, session.id, _new("A12", variant), now=NOW)
    await backend.products.add(other.seller_id, other.id, _new("A12", "M"), now=NOW)

    variants = [str(row.variant) for row in await backend.products.list_for_session(session.id)]
    assert variants == ["S", "M", "L"]


async def test_a_catalog_holds_a_bounded_number_of_products(backend: ReservationBackend) -> None:
    session = await _session(backend)
    for number in range(MAX_PRODUCTS_PER_SESSION):
        await backend.add_product(session.id, f"Z{number}")

    with pytest.raises(ProductConflict) as refused:
        await backend.products.add(session.seller_id, session.id, _new(), now=NOW)

    assert refused.value.conflict is CatalogConflict.FULL
    assert len(await backend.products.list_for_session(session.id)) == MAX_PRODUCTS_PER_SESSION


async def test_another_sellers_catalog_reads_and_writes_as_missing(
    backend: ReservationBackend,
) -> None:
    session = await _session(backend)
    product = await backend.products.add(session.seller_id, session.id, _new(), now=NOW)
    sibling = await backend.add_live_session(external_live_id="live-2", seller_id=session.seller_id)
    other = await backend.add_seller()
    products = backend.products

    assert await products.get_for_seller(other, session.id, product.id) is None
    # The product of one of the seller's sessions, asked for under another of them.
    assert await products.get_for_seller(session.seller_id, sibling.id, product.id) is None
    assert await products.get_for_seller(session.seller_id, session.id, UNKNOWN) is None
    with pytest.raises(RecordNotFound):
        await products.add(other, session.id, _new("B07"), now=NOW)
    with pytest.raises(RecordNotFound):
        await products.list_for_seller(other, session.id, limit=10)
    with pytest.raises(RecordNotFound):
        await products.update(other, session.id, product.id, ProductChanges(name="Stolen"))
    with pytest.raises(RecordNotFound):
        await products.update(
            session.seller_id, sibling.id, product.id, ProductChanges(name="Moved")
        )

    assert await products.list_for_session(session.id) == [product]


async def test_a_seller_lists_a_catalog_in_creation_order_by_page(
    backend: ReservationBackend,
) -> None:
    session = await _session(backend)
    added = [
        await backend.products.add(session.seller_id, session.id, _new(f"B{n}"), now=NOW)
        for n in range(5)
    ]
    products = backend.products

    first = await products.list_for_seller(session.seller_id, session.id, limit=3)
    last = await products.list_for_seller(
        session.seller_id, session.id, limit=3, cursor=first.next_cursor
    )

    assert (first.items, first.next_cursor) == (tuple(added[:3]), added[2].id)
    assert (last.items, last.next_cursor) == (tuple(added[3:]), None)
    empty = await backend.add_live_session(external_live_id="live-2", seller_id=session.seller_id)
    assert (await products.list_for_seller(session.seller_id, empty.id, limit=3)).items == ()


async def test_a_draft_product_changes_everything_but_its_stock(
    backend: ReservationBackend,
) -> None:
    session = await _session(backend)
    product = await backend.products.add(session.seller_id, session.id, _new("A12", "M"), now=NOW)
    changes = ProductChanges(
        code=ProductCode("A13"), variant=Variant("L"), name="Robe longue", price_minor=3_990
    )

    updated = await backend.products.update(session.seller_id, session.id, product.id, changes)

    assert (updated.code, updated.variant, updated.name, updated.price) == (
        ProductCode("A13"),
        Variant("L"),
        "Robe longue",
        Money(3_990, "EUR"),
    )
    assert (updated.id, updated.stock) == (product.id, product.stock)
    assert await backend.products.get(product.id) == updated
    # Alone under its code, a product may drop its variant.
    alone = await backend.products.update(
        session.seller_id, session.id, product.id, ProductChanges(variant=Variant())
    )
    assert alone.variant.is_empty


async def test_a_live_product_changes_its_name_and_price_but_keeps_its_key(
    backend: ReservationBackend,
) -> None:
    session = await _session(backend, LiveSessionStatus.LIVE)
    product = await backend.products.add(session.seller_id, session.id, _new("A12", "M"), now=NOW)
    products = backend.products

    repriced = await products.update(
        session.seller_id,
        session.id,
        product.id,
        # A form that sends the code back unchanged does not move it.
        ProductChanges(code=ProductCode("A12"), name="Robe d'été", price_minor=1_990),
    )
    for changes in (ProductChanges(code=ProductCode("A13")), ProductChanges(variant=Variant("L"))):
        with pytest.raises(EditRefused) as refused:
            await products.update(session.seller_id, session.id, product.id, changes)
        assert refused.value.keys

    assert (repriced.name, repriced.price) == ("Robe d'été", Money(1_990, "EUR"))
    assert await products.get(product.id) == repriced


async def test_an_ended_sessions_products_change_no_more(backend: ReservationBackend) -> None:
    session = await _session(backend)
    product = await backend.products.add(session.seller_id, session.id, _new(), now=NOW)
    await backend.set_session_status(session.id, LiveSessionStatus.ENDED)

    with pytest.raises(EditRefused):
        await backend.products.update(
            session.seller_id, session.id, product.id, ProductChanges(price_minor=1)
        )

    assert await backend.products.get(product.id) == product


async def test_a_product_cannot_move_onto_a_taken_key_or_mix_variants(
    backend: ReservationBackend,
) -> None:
    session = await _session(backend)
    small = await backend.products.add(session.seller_id, session.id, _new("A12", "S"), now=NOW)
    await backend.products.add(session.seller_id, session.id, _new("A12", "M"), now=NOW)
    bag = await backend.products.add(session.seller_id, session.id, _new("B07"), now=NOW)
    products = backend.products

    with pytest.raises(ProductConflict) as taken:
        await products.update(
            session.seller_id, session.id, small.id, ProductChanges(variant=Variant("M"))
        )
    with pytest.raises(ProductConflict) as mixed:
        await products.update(
            session.seller_id, session.id, bag.id, ProductChanges(code=ProductCode("A12"))
        )

    assert taken.value.conflict is CatalogConflict.KEY_TAKEN
    assert mixed.value.conflict is CatalogConflict.VARIANT_MIX
    assert await products.get(small.id) == small
    assert await products.get(bag.id) == bag
