"""What a provider's event about a checkout session does to its payment attempt and its order.

The four ``checkout.session.*`` events LiveCart acts on come down to three
effects: the money came in (``completed`` once paid, and
``async_payment_succeeded``), a delayed payment failed
(``async_payment_failed``), or the session expired unpaid (``expired``).
``decide_payment`` turns an effect and the current statuses into the targets
of the two machines, read under the rows' locks, then written by
compare-and-set:

- the money came in: the attempt ``pending -> succeeded``; the order
  ``pending_payment -> paid``, its reserved units sold, or, if the sweep
  expired it and released its stock first, ``expired -> paid_after_expiry``,
  with no stock effect and a manual refund (the provider's expire-first
  settles that race, so it is a defence in depth). The amount and the
  currency the provider charged must be the order's, or nothing moves;
- a delayed payment failed: the attempt ``pending -> failed``; the order
  keeps its reservation until it ends, for another attempt or the sweep;
- the session expired: the attempt ``pending -> expired``; releasing the
  order's stock is the sweep's, as for any reservation that ended.

An event whose targets are all reached already is a replay; one that finds
its attempt settled otherwise (a ``failed`` after a success, events out of
order) is stale. Neither changes anything, and neither is an error: the
provider's events come at least once, in any order.
"""

from dataclasses import dataclass
from enum import StrEnum

from livecart_core.domain.money import Money
from livecart_core.domain.state_machines import (
    ORDER_TRANSITIONS,
    PAYMENT_TRANSITIONS,
    OrderStatus,
    PaymentStatus,
    TransitionTable,
)


class PaymentEffect(StrEnum):
    """What a checkout session's event says happened to its money."""

    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"


class PaymentVerdict(StrEnum):
    """Why ``decide_payment`` moves what it moves, or nothing."""

    APPLY = "apply"
    REPLAYED = "replayed"
    STALE = "stale"
    AMOUNT_MISMATCH = "amount_mismatch"


@dataclass(frozen=True, slots=True)
class PaymentDecision:
    """The verdict, and the status each row moves to: ``None`` leaves it as it is."""

    verdict: PaymentVerdict
    payment: PaymentStatus | None = None
    order: OrderStatus | None = None

    def __post_init__(self) -> None:
        moves = self.payment is not None or self.order is not None
        if moves != (self.verdict is PaymentVerdict.APPLY):
            raise ValueError("a decision moves a row exactly when its verdict is apply")


_ATTEMPT_TARGETS: dict[PaymentEffect, PaymentStatus] = {
    PaymentEffect.PAID: PaymentStatus.SUCCEEDED,
    PaymentEffect.FAILED: PaymentStatus.FAILED,
    PaymentEffect.EXPIRED: PaymentStatus.EXPIRED,
}


def _move[S: StrEnum](table: TransitionTable[S], current: S, target: S) -> S | None:
    """``target`` when the machine takes the row there now; ``None`` when there or past it."""
    return target if (current, target) in table else None


def _order_target(order: OrderStatus) -> OrderStatus:
    """Where a payment takes an order: paid, or paid after expiry once its stock was released."""
    if order in (OrderStatus.EXPIRED, OrderStatus.PAID_AFTER_EXPIRY):
        return OrderStatus.PAID_AFTER_EXPIRY
    return OrderStatus.PAID


def decide_payment(
    effect: PaymentEffect,
    *,
    payment: PaymentStatus,
    order: OrderStatus,
    charged: Money | None,
    due: Money,
) -> PaymentDecision:
    """The decision of one event; see the module docstring.

    ``charged`` is what the provider says it took, ``due`` the order's total
    in its currency: a payment is refused, and nothing moves, unless both
    are equal. Money that never came in (a failure, an expiry) is not checked.
    """
    if effect is PaymentEffect.PAID and charged != due:
        return PaymentDecision(PaymentVerdict.AMOUNT_MISMATCH)
    attempt_target = _ATTEMPT_TARGETS[effect]
    attempt = _move(PAYMENT_TRANSITIONS, payment, attempt_target)
    moved_order = None
    if effect is PaymentEffect.PAID:
        moved_order = _move(ORDER_TRANSITIONS, order, _order_target(order))
    if attempt is not None or moved_order is not None:
        return PaymentDecision(PaymentVerdict.APPLY, attempt, moved_order)
    if payment is attempt_target:
        return PaymentDecision(PaymentVerdict.REPLAYED)
    return PaymentDecision(PaymentVerdict.STALE)
