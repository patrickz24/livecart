"""Builders of test data: the demo catalog of the parser, and comments as a webhook sends them.

The demo catalog is fixed: the comment corpus (``testing/corpus``) states its
expected results against it, so changing a product here means reviewing the
corpus too.
"""

from datetime import datetime
from typing import Final

from livecart_core.application.dto import IncomingComment, Platform, QueuedComment
from livecart_core.domain.catalog import Catalog, CatalogProduct
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.testing.fakes import FAKE_CLOCK_START

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
