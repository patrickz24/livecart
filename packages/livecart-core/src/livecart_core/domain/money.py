"""Money as integer minor units and an ISO 4217 currency code.

Amounts are minor units (cents for EUR) held in a plain ``int``: no float and
no ``Decimal``, so a price times a quantity is exact and the value maps one to
one onto the ``BIGINT CHECK (>= 0)`` columns. The currency is a three-letter
upper-case code; arithmetic and ordering refuse to mix two currencies, while
equality across currencies is simply false. Nothing here formats an amount for
display: the clients do that from the minor units and the code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from livecart_core.domain.errors import CurrencyMismatch, InvalidValue

_CURRENCY_CODE: Final = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        # A dataclass checks no types at runtime: a float, or a bool, would slip in.
        if type(self.amount_minor) is not int:
            raise InvalidValue(
                f"amount must be an int of minor units, not {type(self.amount_minor).__name__}"
            )
        if self.amount_minor < 0:
            raise InvalidValue(f"amount must not be negative, got {self.amount_minor}")
        if not _CURRENCY_CODE.fullmatch(self.currency):
            raise InvalidValue(
                f"currency must be a three-letter upper-case ISO 4217 code, not {self.currency!r}"
            )

    def __add__(self, other: Money) -> Money:
        return Money(self.amount_minor + self._amount_in(other, "add"), self.currency)

    def __mul__(self, quantity: int) -> Money:
        # Python would happily multiply by a float or a bool; only a count makes sense.
        if type(quantity) is not int:
            return NotImplemented
        return Money(self.amount_minor * quantity, self.currency)

    def __rmul__(self, quantity: int) -> Money:
        return self.__mul__(quantity)

    def __lt__(self, other: Money) -> bool:
        return self.amount_minor < self._amount_in(other, "compare")

    def __le__(self, other: Money) -> bool:
        return self.amount_minor <= self._amount_in(other, "compare")

    def __gt__(self, other: Money) -> bool:
        return self.amount_minor > self._amount_in(other, "compare")

    def __ge__(self, other: Money) -> bool:
        return self.amount_minor >= self._amount_in(other, "compare")

    def _amount_in(self, other: Money, operation: str) -> int:
        """The other amount, once both currencies are known to be the same."""
        if other.currency != self.currency:
            raise CurrencyMismatch(f"cannot {operation} {other.currency} and {self.currency}")
        return other.amount_minor
