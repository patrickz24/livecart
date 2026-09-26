"""``ViewStorefront`` and ``LookUpOrder`` over the fakes: what a buyer reaches, and with what."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.buyer_orders import LookUpOrder
from livecart_core.application.dto import (
    IntentSource,
    LiveSessionRecord,
    OrderRecord,
    ReservationRequest,
    Storefront,
)
from livecart_core.application.storefront import ViewStorefront
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FAKE_CLOCK_START, TEST_ORDER_TOKENS, InMemoryDatabase

NOW = FAKE_CLOCK_START


def _storefront(database: InMemoryDatabase) -> ViewStorefront:
    return ViewStorefront(database.sellers, database.live_sessions, database.products)


def _lookup(database: InMemoryDatabase) -> LookUpOrder:
    return LookUpOrder(database.orders, TEST_ORDER_TOKENS.verify)


async def _order(
    database: InMemoryDatabase, session: LiveSessionRecord, buyer: str = "buyer-1"
) -> OrderRecord:
    """A pending order of B07, opened through unit 3 as the worker will open it."""
    claim = await database.comments.claim(
        queued_comment(external_comment_id=f"comment-{buyer}", author=buyer),
        consumer="worker-1",
        now=NOW,
        lease=timedelta(seconds=30),
    )
    result = await database.orders.reserve(
        ReservationRequest(
            comment_id=claim.comment.id,
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
    )
    assert result is not None
    assert result.order is not None
    return result.order


def _live_session_selling_b07(database: InMemoryDatabase) -> LiveSessionRecord:
    session = database.add_live_session(seller_id=database.add_seller().id)
    database.add_product(session.id, "B07")
    return session


# --- Storefront ------------------------------------------------------------------------


async def test_a_storefront_shows_the_live_session_and_its_products() -> None:
    database = InMemoryDatabase()
    seller = database.add_seller(slug="demo-seller")
    session = database.add_live_session(seller_id=seller.id)
    products = [database.add_product(session.id, "A12"), database.add_product(session.id, "B07")]

    storefront = await _storefront(database)("demo-seller")

    assert storefront == Storefront(seller, session, tuple(products))


async def test_a_storefront_without_a_live_session_shows_the_seller_alone() -> None:
    database = InMemoryDatabase()
    seller = database.add_seller(slug="sample-shop")
    draft = database.add_live_session(seller_id=seller.id, status=LiveSessionStatus.DRAFT)
    database.add_product(draft.id, "A12")

    assert await _storefront(database)("sample-shop") == Storefront(seller)


@pytest.mark.parametrize("slug", ["unknown-shop", "Demo-Seller", "demo seller", "", "x" * 65])
async def test_an_unknown_or_impossible_slug_has_no_storefront(slug: str) -> None:
    database = InMemoryDatabase()
    database.add_seller(slug="demo-seller")

    assert await _storefront(database)(slug) is None


def test_a_storefront_without_a_session_holds_no_products() -> None:
    database = InMemoryDatabase()
    session = database.add_live_session()
    product = database.add_product(session.id, "A12")

    with pytest.raises(ValueError, match="no products"):
        Storefront(database.add_seller(), None, (product,))


# --- Buyer lookup ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed",
    [
        "{reference}",
        "{lower}",
        "{bare}",
        " {reference} ",
    ],
)
async def test_the_reference_alone_finds_the_order_read_tolerantly(typed: str) -> None:
    database = InMemoryDatabase()
    order = await _order(database, _live_session_selling_b07(database))
    reference = order.reference.value
    text = typed.format(
        reference=reference, lower=reference.lower(), bare=reference.replace("-", "")
    )

    found = await _lookup(database).by_reference(text)

    assert found is not None
    assert found.order == order
    assert found.product_code == ProductCode("B07")


@pytest.mark.parametrize("text", ["LC-000000", "LC-UUUUUU", "not a reference", ""])
async def test_an_unknown_or_malformed_reference_finds_nothing(text: str) -> None:
    database = InMemoryDatabase()
    await _order(database, _live_session_selling_b07(database))

    assert await _lookup(database).by_reference(text) is None
    assert (
        await _lookup(database).with_token(text, TEST_ORDER_TOKENS.token_for(UUID(int=1))) is None
    )


async def test_the_detailed_view_needs_the_orders_own_token() -> None:
    database = InMemoryDatabase()
    session = _live_session_selling_b07(database)
    order_a = await _order(database, session, "buyer-a")
    order_b = await _order(database, session, "buyer-b")
    lookup = _lookup(database)
    token_a = TEST_ORDER_TOKENS.token_for(order_a.id)

    opened = await lookup.with_token(order_a.reference.value, token_a)

    assert opened is not None
    assert opened.order == order_a
    assert await lookup.with_token(order_a.reference.value, None) is None
    assert await lookup.with_token(order_a.reference.value, "not-a-token") is None
    altered = ("B" if token_a[0] != "B" else "C") + token_a[1:]
    assert await lookup.with_token(order_a.reference.value, altered) is None
    # The token of order A does not open order B.
    assert await lookup.with_token(order_b.reference.value, token_a) is None
