"""Builders of test data: the demo catalog, comments as a webhook sends them, paid orders.

The demo catalog is fixed: the comment corpus (``testing/corpus``) states its
expected results against it, so changing a product here means reviewing the
corpus too.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from livecart_core.application.dto import (
    IncomingComment,
    IntentSource,
    OrderRecord,
    Platform,
    QueuedComment,
    ReservationRequest,
    ShippingAddress,
)
from livecart_core.domain.catalog import Catalog, CatalogProduct
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus
from livecart_core.testing.fakes import FAKE_CLOCK_START, InMemoryDatabase

# The default MAX_QUANTITY_PER_COMMENT, which the corpus expectations assume.
DEMO_MAX_QUANTITY: Final = 10


def catalog_product(code: str, name: str, *variants: str) -> CatalogProduct:
    """A catalog product from plain strings: ``catalog_product("A12", "Robe", "S", "M")``."""
    return CatalogProduct(ProductCode(code), name, tuple(Variant(label) for label in variants))


def catalog(*products: CatalogProduct) -> Catalog:
    return Catalog(products)


def demo_catalog() -> Catalog:
    """Six products that cover every shape of variant the parser handles.

    - A12: sizes as letters; B07: no variant, and a zero that buyers type as O;
    - C3: colours, one of them in two words ("Bleu nuit" beside "Bleu");
    - D4: sizes as numbers, which compete with quantities;
    - E15: a single variant, which a comment never needs to name;
    - Z99: no variant.
    """
    return catalog(
        catalog_product("A12", "Robe fleurie", "S", "M", "L"),
        catalog_product("B07", "Sac en cuir"),
        catalog_product("C3", "T-shirt coton", "Rouge", "Bleu", "Bleu nuit", "Noir"),
        catalog_product("D4", "Jean droit", "36", "38", "40", "42"),
        catalog_product("E15", "Casquette brodée", "Unique"),
        catalog_product("Z99", "Carte cadeau"),
    )


def incoming_comment(
    text: str = "je prends A12 M",
    *,
    external_comment_id: str = "comment-1",
    author: str = "buyer-1",
    external_live_id: str = "live-1",
    platform: Platform = Platform.SIMULATOR,
    posted_at: datetime = FAKE_CLOCK_START,
) -> IncomingComment:
    """A comment in the common webhook format; the defaults match ``add_live_session()``."""
    return IncomingComment(
        platform=platform,
        external_comment_id=external_comment_id,
        external_live_id=external_live_id,
        author_platform_user_id=author,
        text=text,
        posted_at=posted_at,
    )


def queued_comment(
    text: str = "je prends A12 M",
    *,
    external_comment_id: str = "comment-1",
    author: str = "buyer-1",
    external_live_id: str = "live-1",
    platform: Platform = Platform.SIMULATOR,
    enqueued_at: datetime = FAKE_CLOCK_START,
) -> QueuedComment:
    """``incoming_comment`` in its queue envelope, with ids derived from the comment id."""
    return QueuedComment(
        comment=incoming_comment(
            text,
            external_comment_id=external_comment_id,
            author=author,
            external_live_id=external_live_id,
            platform=platform,
            posted_at=enqueued_at,
        ),
        correlation_id=f"correlation-{external_comment_id}",
        request_id=f"request-{external_comment_id}",
        enqueued_at=enqueued_at,
    )


# Where the test buyers want their parcels delivered.
TEST_ADDRESS: Final = ShippingAddress(
    full_name="Camille Martin",
    line1="12 rue des Lilas",
    postal_code="69003",
    city="Lyon",
    country="FR",
)


async def paid_order(
    database: InMemoryDatabase,
    *,
    comment: str = "comment-1",
    paid_at: datetime = FAKE_CLOCK_START,
    address: ShippingAddress | None = TEST_ADDRESS,
) -> OrderRecord:
    """An order of 2 x B07 at 12.50 EUR, paid at ``paid_at`` and its units sold, to ``address``.

    On the database's first session, created live if there is none, with B07
    added if missing; a buyer of its own per comment. The order is left as
    the registry's settlement leaves a paid one, its attempt and event aside:
    what a shipment needs.
    """
    session = next(iter(database.tables.sessions.values()), None) or database.add_live_session()
    product = database.find_product(session.id, ProductCode("B07"), Variant())
    if product is None:
        product = database.add_product(session.id, "B07", price=Money(1_250, "EUR"))
    envelope = queued_comment(external_comment_id=comment, author=f"buyer-of-{comment}")
    claim = await database.comments.claim(
        envelope, holder="worker-1", now=paid_at, lease=timedelta(seconds=30)
    )
    result = await database.orders.reserve(
        ReservationRequest(
            comment_id=claim.comment.id,
            session_id=session.id,
            product_code=ProductCode("B07"),
            variant=Variant(),
            quantity=Quantity(2),
            source=IntentSource.PARSER,
            confidence=Decimal("1.00"),
            evidence=("B07",),
            now=paid_at,
            reserved_until=paid_at + timedelta(seconds=900),
        )
    )
    if result is None or result.order is None:
        raise AssertionError(f"{comment} reserved nothing")
    order = replace(
        result.order, status=OrderStatus.PAID, paid_at=paid_at, shipping_address=address
    )
    database.tables.orders[order.id] = order
    stock = database.tables.products[product.id].stock
    database.tables.products[product.id] = replace(
        database.tables.products[product.id],
        stock=replace(stock, reserved=stock.reserved - 2, sold=stock.sold + 2),
    )
    return order
