"""Purchase quantities, bounded twice.

``QUANTITY_CEILING`` (99) is the safety bound that ``ck_purchase_intents_quantity``
enforces in the database (milestone 3) and that no configuration may exceed.
The business bound, ``MAX_QUANTITY_PER_COMMENT`` (10 by default), is a
deployment setting: the domain never reads settings, so ``parse_quantity``
receives it as an argument.
"""

from dataclasses import dataclass
from typing import Final

from livecart_core.domain.errors import QuantityOutOfBounds

QUANTITY_CEILING: Final = 99


@dataclass(frozen=True, slots=True)
class Quantity:
    value: int

    def __post_init__(self) -> None:
        # A dataclass checks no types at runtime: a float, or a bool, would slip in.
        if type(self.value) is not int or not 1 <= self.value <= QUANTITY_CEILING:
            raise QuantityOutOfBounds(
                f"quantity must be an int from 1 to {QUANTITY_CEILING}, not {self.value!r}"
            )


def parse_quantity(value: int, *, max_quantity: int) -> Quantity:
    """Validate a purchase quantity against the business bound of the deployment."""
    if not 1 <= max_quantity <= QUANTITY_CEILING:
        # A caller bug, not bad input: the settings validator keeps the bound in range.
        raise ValueError(f"max_quantity must be from 1 to {QUANTITY_CEILING}, not {max_quantity}")
    if type(value) is not int or not 1 <= value <= max_quantity:
        raise QuantityOutOfBounds(f"quantity must be from 1 to {max_quantity}, not {value!r}")
    return Quantity(value)
