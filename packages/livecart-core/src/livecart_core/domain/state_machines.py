"""Status enumerations and their transition tables, one per aggregate.

Each status is a ``StrEnum`` whose values are the exact strings of the
database ``CHECK`` constraints, the metrics labels and the dashboard. Each
table is a frozen set of the allowed (current, target) pairs, and
``transition`` is the one rule that reads them: a state that never appears as
a source is terminal. The persistence layer (milestone 3) applies a
transition as a compare-and-set ``UPDATE ... WHERE status = :current`` and
maps its row count onto the same outcomes: ``APPLIED``, ``NOOP`` when the row
already holds the target (a replayed event, such as a payment webhook
delivered twice), or ``InvalidTransition``.
"""

from enum import StrEnum
from typing import Final

from livecart_core.domain.errors import InvalidTransition


class TransitionOutcome(StrEnum):
    APPLIED = "applied"
    NOOP = "noop"


type TransitionTable[S: StrEnum] = frozenset[tuple[S, S]]


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    EXPIRED = "expired"
    # A payment confirmed after the sweep released the stock: no stock effect,
    # a badge on the dashboard and a manual refund by runbook.
    PAID_AFTER_EXPIRY = "paid_after_expiry"


ORDER_TRANSITIONS: Final[TransitionTable[OrderStatus]] = frozenset(
    {
        (OrderStatus.PENDING_PAYMENT, OrderStatus.PAID),
        (OrderStatus.PENDING_PAYMENT, OrderStatus.EXPIRED),
        (OrderStatus.EXPIRED, OrderStatus.PAID_AFTER_EXPIRY),
    }
)


class PaymentStatus(StrEnum):
    """The status of one payment attempt; an order has at most one pending attempt."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"


PAYMENT_TRANSITIONS: Final[TransitionTable[PaymentStatus]] = frozenset(
    {
        (PaymentStatus.PENDING, PaymentStatus.SUCCEEDED),
        (PaymentStatus.PENDING, PaymentStatus.FAILED),
        (PaymentStatus.PENDING, PaymentStatus.EXPIRED),
    }
)


class ShipmentStatus(StrEnum):
    CREATED = "created"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


SHIPMENT_TRANSITIONS: Final[TransitionTable[ShipmentStatus]] = frozenset(
    {
        (ShipmentStatus.CREATED, ShipmentStatus.IN_TRANSIT),
        (ShipmentStatus.IN_TRANSIT, ShipmentStatus.OUT_FOR_DELIVERY),
        (ShipmentStatus.OUT_FOR_DELIVERY, ShipmentStatus.DELIVERED),
        (ShipmentStatus.CREATED, ShipmentStatus.CANCELLED),
    }
)


class LiveSessionStatus(StrEnum):
    DRAFT = "draft"
    LIVE = "live"
    ENDED = "ended"


LIVE_SESSION_TRANSITIONS: Final[TransitionTable[LiveSessionStatus]] = frozenset(
    {
        (LiveSessionStatus.DRAFT, LiveSessionStatus.LIVE),
        (LiveSessionStatus.LIVE, LiveSessionStatus.ENDED),
    }
)


class CommentStatus(StrEnum):
    """The inbox row of a comment: received once, then one terminal outcome."""

    RECEIVED = "received"
    PROCESSED = "processed"
    REJECTED = "rejected"
    DEAD_LETTERED = "dead_lettered"


COMMENT_TRANSITIONS: Final[TransitionTable[CommentStatus]] = frozenset(
    {
        (CommentStatus.RECEIVED, CommentStatus.PROCESSED),
        (CommentStatus.RECEIVED, CommentStatus.REJECTED),
        (CommentStatus.RECEIVED, CommentStatus.DEAD_LETTERED),
    }
)


def transition[S: StrEnum](table: TransitionTable[S], current: S, target: S) -> TransitionOutcome:
    """Decide one status change: APPLIED, NOOP when already there, else InvalidTransition."""
    if current == target:
        return TransitionOutcome.NOOP
    if (current, target) not in table:
        raise InvalidTransition(current, target)
    return TransitionOutcome.APPLIED
