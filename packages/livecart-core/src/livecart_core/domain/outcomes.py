"""Closed lists of the comment pipeline, persisted as ``VARCHAR`` with a ``CHECK``.

The database ``CHECK`` constraints, the metrics labels and the dashboard's
badges use these exact strings, so a new value is added here first and then
to each of them. The parser yields a rejection or an ambiguity reason, the
LLM fallback records an outcome on every attempt, and the stock reservation
records what became of a resolved intent.
"""

from enum import StrEnum


class RejectionReason(StrEnum):
    """Why a comment carries no order, stored in ``comments.rejection_reason``."""

    # Decided by the parser.
    NO_INTENT = "no_intent"
    MULTIPLE_PRODUCTS = "multiple_products"
    QUANTITY_OUT_OF_BOUNDS = "quantity_out_of_bounds"
    VARIANT_NOT_FOUND = "variant_not_found"
    # An ambiguity the LLM fallback could not settle: disabled, failed, busy or over budget.
    AMBIGUOUS_UNRESOLVED = "ambiguous_unresolved"
    # Decided by session resolution, before the parser runs.
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_NOT_LIVE = "session_not_live"


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
