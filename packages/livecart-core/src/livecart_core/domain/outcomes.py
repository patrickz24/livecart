"""Closed lists of the comment pipeline, persisted as ``VARCHAR`` with a ``CHECK``.

The database ``CHECK`` constraints, the metrics labels and the dashboard's
badges use these exact strings, so a new value is added here first and then
to each of them. The parser yields a rejection or an ambiguity reason, the
LLM fallback records an outcome on every attempt and, when the model
answered, the reason it gave, and the stock reservation records what became
of a resolved intent.
"""

from enum import StrEnum
from typing import Final


class RejectionReason(StrEnum):
    """Why a comment carries no order, stored in ``comments.rejection_reason``."""

    # Decided by the parser.
    NO_INTENT = "no_intent"
    MULTIPLE_PRODUCTS = "multiple_products"
    QUANTITY_OUT_OF_BOUNDS = "quantity_out_of_bounds"
    VARIANT_NOT_FOUND = "variant_not_found"
    # An ambiguity the LLM fallback did not settle: disabled, failed, busy, over
    # budget, or read by the model as no order the catalog can take.
    AMBIGUOUS_UNRESOLVED = "ambiguous_unresolved"
    # Decided by session resolution, before the parser runs.
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_NOT_LIVE = "session_not_live"


# The rejections the parser itself decides, in the order of its rules: what
# comments.parse_reason may hold beside the ambiguity reasons. A tuple, so
# that the CHECK built from it lists them in a stable order.
PARSER_REJECTIONS: Final = (
    RejectionReason.NO_INTENT,
    RejectionReason.MULTIPLE_PRODUCTS,
    RejectionReason.QUANTITY_OUT_OF_BOUNDS,
    RejectionReason.VARIANT_NOT_FOUND,
)


class AmbiguityReason(StrEnum):
    """What the parser could not settle: the only cases sent to the LLM."""

    UNKNOWN_CODE = "unknown_code"
    PRODUCT_BY_NAME = "product_by_name"
    UNRESOLVED_VARIANT = "unresolved_variant"
    CONFLICTING_SIGNALS = "conflicting_signals"


class IntentOutcome(StrEnum):
    """What the stock reservation did with a resolved intent."""

    RESERVED = "reserved"
    OUT_OF_STOCK = "out_of_stock"
    ALREADY_RESERVED = "already_reserved"


class LlmOutcome(StrEnum):
    """The receipt of one LLM fallback attempt, stored in ``comments.llm_outcome``."""

    OK = "ok"
    DISABLED = "disabled"
    BUDGET_EXCEEDED = "budget_exceeded"
    LLM_BUSY = "llm_busy"
    TIMEOUT = "timeout"
    ERROR = "error"
    REFUSED = "refused"
    TRUNCATED = "truncated"
    INVALID = "invalid"


class LlmReason(StrEnum):
    """The reason the model gives for its reading of a comment: ``comments.llm_reason``.

    Part of its structured answer, so that the model says why a comment
    orders nothing without a word of free text. Only ``order`` names a
    product, and even then the domain checks it (``decide_from_llm``).
    """

    ORDER = "order"
    # A question, a negation, a purchase already made, a remark.
    NO_ORDER = "no_order"
    UNKNOWN_PRODUCT = "unknown_product"
    UNCLEAR_VARIANT = "unclear_variant"
    UNCLEAR_QUANTITY = "unclear_quantity"
    SEVERAL_PRODUCTS = "several_products"
