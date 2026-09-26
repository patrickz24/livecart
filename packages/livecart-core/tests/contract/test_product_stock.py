"""``ProductStockRepository`` on every backend: the in-memory fake, then PostgreSQL."""

from uuid import UUID

import pytest

from livecart_core.application.dto import StockLevel
from livecart_core.application.ports import RecordNotFound, StockConflict
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.testing.backends import ReservationBackend

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

    added = await backend.products.restock(product.id, 5)
    removed = await backend.products.restock(product.id, -12)

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

    assert (await backend.products.restock(product.id, -5)).stock.available == 0
    with pytest.raises(StockConflict, match="below"):
        await backend.products.restock(product.id, -1)
    with pytest.raises(RecordNotFound):
        await backend.products.restock(UNKNOWN, 1)
    stored = await backend.products.get(product.id)
    assert stored is not None
    assert stored.stock == StockLevel(5, reserved=3, sold=2)
