"""What the parser says about one comment: a match, an ambiguity or a rejection."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.outcomes import AmbiguityReason, RejectionReason
from livecart_core.domain.quantity import Quantity


class Confidence(Enum):
    """The closed scale of a parser match, from the weakest evidence it relied on.

    Decimal values, so that they land exactly in a ``NUMERIC`` column next to
    the LLM's own confidence.
    """

    # A buy signal (a verb or the cart emoji) and a code, both as typed.
    EXACT = Decimal("1.00")
    # A code typed as is, with no buy signal: the "code alone" form.
    CODE_ALONE = Decimal("0.90")
    # A buy verb read through one typo, or a code read through an O/0 or I/1 swap.
    CORRECTED = Decimal("0.80")


@dataclass(frozen=True, slots=True)
class Match:
    """One product, one variant (``NO_VARIANT`` when it has none) and a bounded quantity."""

    product_code: ProductCode
    variant: Variant
    quantity: Quantity
    confidence: Confidence
    # The substrings of the original comment that the match rests on, in text order.
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """A comment the parser could not settle alone: the only kind the LLM fallback sees."""

    reason: AmbiguityReason
    # The catalog codes in play, when the parser narrowed them down; may be empty.
    candidates: tuple[ProductCode, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Rejected:
    """A comment that carries no order, whatever an LLM could read into it."""

    reason: RejectionReason


type ParseResult = Match | Ambiguous | Rejected
