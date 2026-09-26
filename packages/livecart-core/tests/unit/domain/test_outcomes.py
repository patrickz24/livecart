from enum import StrEnum

import pytest

from livecart_core.domain.outcomes import (
    AmbiguityReason,
    IntentOutcome,
    LlmOutcome,
    LlmReason,
    RejectionReason,
)


def test_rejection_reasons_match_the_plan_exactly() -> None:
    assert [reason.value for reason in RejectionReason] == [
        "no_intent",
        "multiple_products",
        "quantity_out_of_bounds",
        "variant_not_found",
        "ambiguous_unresolved",
        "session_not_found",
        "session_not_live",
    ]


def test_ambiguity_reasons_match_the_plan_exactly() -> None:
    assert [reason.value for reason in AmbiguityReason] == [
        "unknown_code",
        "product_by_name",
        "unresolved_variant",
        "conflicting_signals",
    ]


def test_intent_outcomes_match_the_plan_exactly() -> None:
    assert [outcome.value for outcome in IntentOutcome] == [
        "reserved",
        "out_of_stock",
        "already_reserved",
    ]


def test_llm_outcomes_match_the_plan_exactly() -> None:
    assert [outcome.value for outcome in LlmOutcome] == [
        "ok",
        "disabled",
        "budget_exceeded",
        "llm_busy",
        "timeout",
        "error",
        "refused",
        "truncated",
        "invalid",
    ]


def test_the_reasons_the_model_gives_are_a_closed_list() -> None:
    assert [reason.value for reason in LlmReason] == [
        "order",
        "no_order",
        "unknown_product",
        "unclear_variant",
        "unclear_quantity",
        "several_products",
    ]


@pytest.mark.parametrize(
    "enum", [RejectionReason, AmbiguityReason, IntentOutcome, LlmOutcome, LlmReason]
)
def test_the_lists_are_string_enums_that_render_as_their_value(enum: type[StrEnum]) -> None:
    assert issubclass(enum, StrEnum)
    for member in enum:
        assert f"{member}" == member.value
        assert enum(member.value) is member


def test_a_rejection_reason_and_an_ambiguity_reason_never_share_a_value() -> None:
    assert not {r.value for r in RejectionReason} & {r.value for r in AmbiguityReason}
