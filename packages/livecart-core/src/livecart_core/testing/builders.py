"""Builders of domain objects for tests, starting with the demo catalog of the parser.

The demo catalog is fixed: the comment corpus (``testing/corpus``) states its
expected results against it, so changing a product here means reviewing the
corpus too.
"""

from typing import Final

from livecart_core.domain.catalog import Catalog, CatalogProduct
from livecart_core.domain.codes import ProductCode, Variant

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
