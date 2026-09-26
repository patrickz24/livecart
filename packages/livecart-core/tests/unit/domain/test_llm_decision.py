from decimal import Decimal

import pytest

from livecart_core.domain.codes import NO_VARIANT, ProductCode, Variant
from livecart_core.domain.llm_decision import (
    LLM_CONFIDENCE_FLOOR,
    LlmChoice,
    LlmProposal,
    decide_from_llm,
)
from livecart_core.domain.outcomes import LlmReason
from livecart_core.domain.quantity import Quantity
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, demo_catalog

CATALOG = demo_catalog()
# FULLWIDTH LATIN SMALL LETTER C, FULLWIDTH DIGIT THREE: "c3" on a CJK keyboard.
FULLWIDTH_LOWER_C3 = chr(0xFF43) + chr(0xFF13)


def order(
    code: str | None,
    variant: str | None = None,
    quantity: int | None = 1,
    confidence: str = "0.90",
) -> LlmProposal:
    return LlmProposal(LlmReason.ORDER, code, variant, quantity, Decimal(confidence))


def decide(proposal: LlmProposal) -> LlmChoice | None:
    return decide_from_llm(proposal, CATALOG, max_quantity=DEMO_MAX_QUANTITY)


def test_an_order_the_catalog_holds_becomes_a_choice() -> None:
    assert decide(order("C3", "Bleu nuit", 2, "0.87")) == LlmChoice(
        ProductCode("C3"), Variant("Bleu nuit"), Quantity(2), Decimal("0.87")
    )


def test_a_code_and_a_variant_are_read_as_a_buyer_types_them() -> None:
    choice = decide(order(f" {FULLWIDTH_LOWER_C3} ", "bleu NUIT"))

    assert choice is not None
    assert (choice.product_code, choice.variant) == (ProductCode("C3"), Variant("Bleu nuit"))


@pytest.mark.parametrize("variant", [None, "", "  "])
def test_a_product_without_variants_takes_none(variant: str | None) -> None:
    choice = decide(order("B07", variant))

    assert choice is not None
    assert choice.variant == NO_VARIANT


@pytest.mark.parametrize("variant", [None, "unique"])
def test_a_product_with_a_single_variant_takes_it(variant: str | None) -> None:
    choice = decide(order("E15", variant))

    assert choice is not None
    assert choice.variant == Variant("Unique")


@pytest.mark.parametrize(
    "proposal",
    [
        pytest.param(order("Q7"), id="a code the catalog does not list"),
        pytest.param(order("A12 M"), id="no code at all"),
        pytest.param(order(None), id="an order without a code"),
        pytest.param(order("A12"), id="a product of several variants, none named"),
        pytest.param(order("A12", "XL"), id="a variant the product does not have"),
        pytest.param(order("B07", "Noir"), id="a variant for a product without any"),
        pytest.param(order("B07", None, None), id="an order without a quantity"),
        pytest.param(order("Z99", None, 0), id="nothing to order"),
        pytest.param(order("Z99", None, DEMO_MAX_QUANTITY + 1), id="above the business bound"),
        pytest.param(order("Z99", None, 99), id="the injected 99"),
        pytest.param(order("B07", confidence="0.49"), id="below the floor"),
        pytest.param(order("B07", confidence="1.01"), id="above certainty"),
    ],
)
def test_a_proposal_that_does_not_hold_settles_nothing(proposal: LlmProposal) -> None:
    assert decide(proposal) is None


@pytest.mark.parametrize("reason", [reason for reason in LlmReason if reason != LlmReason.ORDER])
def test_only_an_order_settles_an_intent(reason: LlmReason) -> None:
    # Even with a valid product: the model said the comment orders nothing.
    proposal = LlmProposal(reason, "B07", None, 1, Decimal("0.95"))

    assert decide(proposal) is None


def test_the_floor_itself_is_enough() -> None:
    assert decide(order("B07", confidence=str(LLM_CONFIDENCE_FLOOR))) is not None


def test_a_quantity_up_to_the_bound_in_force_is_accepted() -> None:
    proposal = order("Z99", None, 25)

    assert decide_from_llm(proposal, CATALOG, max_quantity=DEMO_MAX_QUANTITY) is None
    choice = decide_from_llm(proposal, CATALOG, max_quantity=25)
    assert choice is not None
    assert choice.quantity == Quantity(25)
