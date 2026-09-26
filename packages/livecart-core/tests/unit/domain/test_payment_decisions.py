"""``decide_payment`` over every effect, attempt status and order status."""

import itertools

import pytest

from livecart_core.domain.money import Money
from livecart_core.domain.payments import (
    PaymentDecision,
    PaymentEffect,
    PaymentVerdict,
    decide_payment,
)
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus

DUE = Money(2_500, "EUR")

# (effect, attempt, order) -> (verdict, attempt's target, order's target), for the right amount.
type Expected = tuple[PaymentVerdict, PaymentStatus | None, OrderStatus | None]
EXPECTED: dict[tuple[PaymentEffect, PaymentStatus, OrderStatus], Expected] = {}
for attempt, order in itertools.product(PaymentStatus, OrderStatus):
    pending = attempt is PaymentStatus.PENDING
    # The money came in: the attempt succeeds if it can, the order is paid.
    order_target = {
        OrderStatus.PENDING_PAYMENT: OrderStatus.PAID,
        OrderStatus.EXPIRED: OrderStatus.PAID_AFTER_EXPIRY,
    }.get(order)
    attempt_target = PaymentStatus.SUCCEEDED if pending else None
    paid: Expected
    if attempt_target is not None or order_target is not None:
        paid = (PaymentVerdict.APPLY, attempt_target, order_target)
    elif attempt is PaymentStatus.SUCCEEDED:
        paid = (PaymentVerdict.REPLAYED, None, None)
    else:
        paid = (PaymentVerdict.STALE, None, None)
    EXPECTED[(PaymentEffect.PAID, attempt, order)] = paid
    # A failure or an expiry settles a pending attempt only, and never the order.
    for effect, target in (
        (PaymentEffect.FAILED, PaymentStatus.FAILED),
        (PaymentEffect.EXPIRED, PaymentStatus.EXPIRED),
    ):
        outcome: Expected
        if pending:
            outcome = (PaymentVerdict.APPLY, target, None)
        elif attempt is target:
            outcome = (PaymentVerdict.REPLAYED, None, None)
        else:
            outcome = (PaymentVerdict.STALE, None, None)
        EXPECTED[(effect, attempt, order)] = outcome


@pytest.mark.parametrize(
    ("effect", "attempt", "order"),
    list(EXPECTED),
    ids=[f"{e}-{a}-{o}" for e, a, o in EXPECTED],
)
def test_each_event_moves_what_its_machines_allow(
    effect: PaymentEffect, attempt: PaymentStatus, order: OrderStatus
) -> None:
    decision = decide_payment(effect, payment=attempt, order=order, charged=DUE, due=DUE)

    assert (decision.verdict, decision.payment, decision.order) == EXPECTED[
        (effect, attempt, order)
    ]


def test_every_combination_is_covered() -> None:
    assert len(EXPECTED) == len(PaymentEffect) * len(PaymentStatus) * len(OrderStatus)


@pytest.mark.parametrize(
    "charged",
    [Money(2_499, "EUR"), Money(2_501, "EUR"), Money(2_500, "USD"), None],
    ids=["less", "more", "currency", "none"],
)
@pytest.mark.parametrize("order", list(OrderStatus))
def test_a_payment_of_another_amount_moves_nothing(
    charged: Money | None, order: OrderStatus
) -> None:
    decision = decide_payment(
        PaymentEffect.PAID, payment=PaymentStatus.PENDING, order=order, charged=charged, due=DUE
    )

    assert decision == PaymentDecision(PaymentVerdict.AMOUNT_MISMATCH)


@pytest.mark.parametrize("effect", [PaymentEffect.FAILED, PaymentEffect.EXPIRED])
def test_money_that_never_came_in_is_not_checked(effect: PaymentEffect) -> None:
    decision = decide_payment(
        effect,
        payment=PaymentStatus.PENDING,
        order=OrderStatus.PENDING_PAYMENT,
        charged=None,
        due=DUE,
    )

    assert decision.verdict is PaymentVerdict.APPLY


def test_a_decision_moves_a_row_exactly_when_it_applies() -> None:
    with pytest.raises(ValueError, match="exactly when"):
        PaymentDecision(PaymentVerdict.APPLY)
    with pytest.raises(ValueError, match="exactly when"):
        PaymentDecision(PaymentVerdict.REPLAYED, payment=PaymentStatus.SUCCEEDED)
