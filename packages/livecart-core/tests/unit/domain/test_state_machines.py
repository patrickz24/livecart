from enum import StrEnum
from itertools import product
from typing import Final

import pytest

from livecart_core.domain.errors import DomainError, InvalidTransition
from livecart_core.domain.state_machines import (
    COMMENT_TRANSITIONS,
    LIVE_SESSION_TRANSITIONS,
    ORDER_TRANSITIONS,
    PAYMENT_TRANSITIONS,
    SHIPMENT_TRANSITIONS,
    CommentStatus,
    LiveSessionStatus,
    OrderStatus,
    PaymentStatus,
    ShipmentStatus,
    TransitionOutcome,
    TransitionTable,
    transition,
)

# Every machine of the plan, in the order of its "Machines à états" table.
MACHINES: Final[list[tuple[type[StrEnum], TransitionTable[StrEnum]]]] = [
    (OrderStatus, ORDER_TRANSITIONS),
    (PaymentStatus, PAYMENT_TRANSITIONS),
    (ShipmentStatus, SHIPMENT_TRANSITIONS),
    (LiveSessionStatus, LIVE_SESSION_TRANSITIONS),
    (CommentStatus, COMMENT_TRANSITIONS),
]

Case = tuple[TransitionTable[StrEnum], StrEnum, StrEnum]


def _case(table: TransitionTable[StrEnum], current: StrEnum, target: StrEnum) -> object:
    return pytest.param(
        (table, current, target), id=f"{type(current).__name__}:{current}->{target}"
    )


def _allowed_cases() -> list[object]:
    return [
        _case(table, current, target) for _, table in MACHINES for current, target in sorted(table)
    ]


def _forbidden_cases() -> list[object]:
    """Every ordered pair of distinct statuses the table leaves out, per machine."""
    return [
        _case(table, current, target)
        for statuses, table in MACHINES
        for current, target in product(statuses, repeat=2)
        if current != target and (current, target) not in table
    ]


def _noop_cases() -> list[object]:
    return [_case(table, status, status) for statuses, table in MACHINES for status in statuses]


def _pairs(table: TransitionTable[StrEnum]) -> set[tuple[str, str]]:
    return {(current.value, target.value) for current, target in table}


@pytest.mark.parametrize("case", _allowed_cases())
def test_every_transition_of_the_plan_is_applied(case: Case) -> None:
    table, current, target = case

    assert transition(table, current, target) is TransitionOutcome.APPLIED


@pytest.mark.parametrize("case", _forbidden_cases())
def test_every_other_pair_is_an_invalid_transition(case: Case) -> None:
    table, current, target = case

    with pytest.raises(InvalidTransition) as excinfo:
        transition(table, current, target)

    assert excinfo.value.current is current
    assert excinfo.value.target is target


@pytest.mark.parametrize("case", _noop_cases())
def test_reaching_the_current_state_again_is_a_noop(case: Case) -> None:
    table, current, target = case

    assert transition(table, current, target) is TransitionOutcome.NOOP


def test_the_exhaustive_cases_cover_every_ordered_pair() -> None:
    total = sum(len(statuses) ** 2 for statuses, _ in MACHINES)

    assert len(_allowed_cases()) + len(_forbidden_cases()) + len(_noop_cases()) == total
    assert len(_allowed_cases()) == 3 + 3 + 4 + 2 + 3


def test_an_invalid_transition_names_the_machine_and_both_states() -> None:
    with pytest.raises(InvalidTransition, match="OrderStatus: paid -> expired is not allowed"):
        transition(ORDER_TRANSITIONS, OrderStatus.PAID, OrderStatus.EXPIRED)


def test_an_invalid_transition_is_a_domain_error() -> None:
    assert issubclass(InvalidTransition, DomainError)


def test_transition_outcomes_are_loggable_strings() -> None:
    assert [outcome.value for outcome in TransitionOutcome] == ["applied", "noop"]
    assert f"{TransitionOutcome.NOOP}" == "noop"


@pytest.mark.parametrize(("_statuses", "table"), MACHINES)
def test_the_tables_are_frozen(_statuses: type[StrEnum], table: TransitionTable[StrEnum]) -> None:
    assert isinstance(table, frozenset)
    assert not hasattr(table, "add")


def test_order_transitions_match_the_plan() -> None:
    assert _pairs(ORDER_TRANSITIONS) == {
        ("pending_payment", "paid"),
        ("pending_payment", "expired"),
        ("expired", "paid_after_expiry"),
    }


def test_payment_transitions_match_the_plan() -> None:
    assert _pairs(PAYMENT_TRANSITIONS) == {
        ("pending", "succeeded"),
        ("pending", "failed"),
        ("pending", "expired"),
    }


def test_shipment_transitions_match_the_plan() -> None:
    assert _pairs(SHIPMENT_TRANSITIONS) == {
        ("created", "in_transit"),
        ("in_transit", "out_for_delivery"),
        ("out_for_delivery", "delivered"),
        ("created", "cancelled"),
    }


def test_live_session_transitions_match_the_plan() -> None:
    assert _pairs(LIVE_SESSION_TRANSITIONS) == {("draft", "live"), ("live", "ended")}


def test_comment_transitions_match_the_plan() -> None:
    assert _pairs(COMMENT_TRANSITIONS) == {
        ("received", "processed"),
        ("received", "rejected"),
        ("received", "dead_lettered"),
    }


def test_status_values_match_the_database_checks() -> None:
    assert [s.value for s in OrderStatus] == [
        "pending_payment",
        "paid",
        "expired",
        "paid_after_expiry",
    ]
    assert [s.value for s in PaymentStatus] == ["pending", "succeeded", "failed", "expired"]
    assert [s.value for s in ShipmentStatus] == [
        "created",
        "in_transit",
        "out_for_delivery",
        "delivered",
        "cancelled",
    ]
    assert [s.value for s in LiveSessionStatus] == ["draft", "live", "ended"]
    assert [s.value for s in CommentStatus] == [
        "received",
        "processed",
        "rejected",
        "dead_lettered",
    ]


def test_statuses_render_as_their_database_value() -> None:
    assert f"{OrderStatus.PAID_AFTER_EXPIRY}" == "paid_after_expiry"
    assert str(OrderStatus.PAID) == "paid"
    assert OrderStatus("paid") is OrderStatus.PAID
