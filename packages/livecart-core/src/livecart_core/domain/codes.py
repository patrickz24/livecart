"""Product codes and variants, as the seller declares them and as buyers type them.

A product code is one to three letters then one to four digits (``A12``,
``C3``, ``XYZ1234``), the shape of ``ck_products_code``. Buyers type them in
every possible way (lower case, fullwidth characters, surrounding spaces), so
``parse_product_code`` normalises before checking the shape, while the
``ProductCode`` constructor takes the canonical form only.

A variant is the seller's free label for a size or a colour, 32 characters at
most (the width of ``products.variant``). The empty string means "no variant",
never ``None``: the unique index on (session, code, variant) would let two
``NULL`` rows through.
"""

import re
from dataclasses import dataclass
from typing import Final

from livecart_core.domain.errors import InvalidProductCode, InvalidValue
from livecart_core.domain.text import normalize_text

# Kept in step with ck_products_code and the width of products.code (VARCHAR(7)).
PRODUCT_CODE_PATTERN: Final = re.compile(r"[A-Z]{1,3}[0-9]{1,4}")
# The width of products.variant, counted in code points like VARCHAR does.
MAX_VARIANT_LENGTH: Final = 32


@dataclass(frozen=True, slots=True)
class ProductCode:
    value: str

    def __post_init__(self) -> None:
        if not PRODUCT_CODE_PATTERN.fullmatch(self.value):
            raise InvalidProductCode(
                f"{self.value!r} is not a product code (1 to 3 letters then 1 to 4 digits)"
            )

    def __str__(self) -> str:
        return self.value


def parse_product_code(text: str) -> ProductCode:
    """Read a product code as typed: NFKC, strip and upper-case, then check the shape."""
    return ProductCode(normalize_text(text).upper())


@dataclass(frozen=True, slots=True)
class Variant:
    value: str = ""

    def __post_init__(self) -> None:
        if len(self.value) > MAX_VARIANT_LENGTH:
            raise InvalidValue(f"variant is longer than {MAX_VARIANT_LENGTH} characters")
        # Surrounding whitespace would make two labels look the same yet index apart.
        if self.value != self.value.strip():
            raise InvalidValue(f"variant {self.value!r} starts or ends with whitespace")

    @property
    def is_empty(self) -> bool:
        return self.value == ""

    def __str__(self) -> str:
        return self.value


NO_VARIANT: Final = Variant()


def parse_variant(text: str) -> Variant:
    """Read a variant label as typed: NFKC and strip, keeping the seller's case."""
    return Variant(normalize_text(text))
