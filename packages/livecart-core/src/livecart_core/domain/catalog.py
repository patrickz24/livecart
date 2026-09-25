"""The products of one live session, as the comment parser sees them.

The database holds one ``products`` row per code and variant (milestone 3);
the parser needs them grouped by code: the code a buyer types, the name a
buyer may use instead, and the variant labels, if the product has any. The
application layer builds a ``Catalog`` from the rows of the session before it
parses a comment, and the catalog never changes during a parse.

A product without variants has an empty ``variants`` tuple, never a tuple
holding the empty variant: "no variant" is the absence of a choice, not a
choice. Two labels of one product that differ only by case would be the same
label to a buyer typing it, so they are refused.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.errors import InvalidValue


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    code: ProductCode
    name: str
    variants: tuple[Variant, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidValue(f"product {self.code} needs a name")
        if any(variant.is_empty for variant in self.variants):
            raise InvalidValue(
                f"product {self.code}: a product without variants has no variant labels at all"
            )
        folded = [variant.value.casefold() for variant in self.variants]
        if len(set(folded)) != len(folded):
            raise InvalidValue(f"product {self.code} lists the same variant label twice")

    @property
    def has_variants(self) -> bool:
        return bool(self.variants)


@dataclass(frozen=True, slots=True)
class Catalog:
    products: tuple[CatalogProduct, ...]
    # A read-only index by code, left out of equality, hashing and repr: the
    # products tuple is the value.
    _by_code: Mapping[ProductCode, CatalogProduct] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_code: dict[ProductCode, CatalogProduct] = {}
        for product in self.products:
            if product.code in by_code:
                raise InvalidValue(f"the catalog lists product {product.code} twice")
            by_code[product.code] = product
        # The dataclass is frozen: the index is set once, here.
        object.__setattr__(self, "_by_code", MappingProxyType(by_code))

    def get(self, code: ProductCode) -> CatalogProduct | None:
        return self._by_code.get(code)

    def __contains__(self, code: object) -> bool:
        return code in self._by_code

    def __iter__(self) -> Iterator[CatalogProduct]:
        return iter(self.products)

    def __len__(self) -> int:
        return len(self.products)
