"""The parser against its French and English corpus, case by case and field by field."""

from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from livecart_core.domain.llm_decision import LlmProposal, decide_from_llm
from livecart_core.domain.outcomes import LlmReason
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, demo_catalog
from livecart_core.testing.corpus import (
    AmbiguousCase,
    CorpusError,
    MatchCase,
    RejectedCase,
    load_corpus,
    mismatches,
)

CORPUS = load_corpus()


def test_the_corpus_has_its_planned_size() -> None:
    kinds = Counter(line.case.kind for line in CORPUS)

    assert 150 <= len(CORPUS) <= 200
    assert kinds["ambiguous"] >= 40
    assert kinds["match"] > 0
    assert kinds["rejected"] > 0


def test_every_case_gives_exactly_the_expected_result() -> None:
    failures = mismatches(CORPUS, demo_catalog(), max_quantity=DEMO_MAX_QUANTITY)

    assert not failures, f"{len(failures)} corpus case(s) differ:\n" + "\n".join(failures)


def test_every_resolution_is_an_order_the_domain_takes_from_the_llm() -> None:
    # A label the domain would refuse could never be reached: the evaluation
    # of the fallback would count a miss the model cannot avoid.
    resolved = [
        line.case
        for line in CORPUS
        if isinstance(line.case, AmbiguousCase) and line.case.resolution is not None
    ]
    for case in resolved:
        assert case.resolution is not None
        proposal = LlmProposal(
            LlmReason.ORDER,
            case.resolution.code,
            case.resolution.variant,
            case.resolution.quantity,
            Decimal(1),
        )
        choice = decide_from_llm(proposal, demo_catalog(), max_quantity=DEMO_MAX_QUANTITY)
        assert choice is not None, case.text
        assert (str(choice.product_code), str(choice.variant), choice.quantity.value) == (
            case.resolution.code,
            case.resolution.variant,
            case.resolution.quantity,
        ), case.text
    # Some ambiguities carry an order, and the rest none: both are measured.
    ambiguous = sum(isinstance(line.case, AmbiguousCase) for line in CORPUS)
    assert 0 < len(resolved) < ambiguous


def test_every_outcome_of_the_parser_appears_in_the_corpus() -> None:
    reasons = {
        str(line.case.reason)
        for line in CORPUS
        if isinstance(line.case, AmbiguousCase | RejectedCase)
    }
    confidences = {line.case.confidence for line in CORPUS if isinstance(line.case, MatchCase)}

    assert reasons == {
        "no_intent",
        "multiple_products",
        "quantity_out_of_bounds",
        "variant_not_found",
        "unknown_code",
        "product_by_name",
        "unresolved_variant",
        "conflicting_signals",
    }
    assert confidences == {"1.00", "0.90", "0.80"}


def test_the_cases_the_plan_names_are_in_the_corpus() -> None:
    texts = {line.case.text for line in CORPUS}
    keycap_two = "2" + chr(0xFE0F) + chr(0x20E3)
    fullwidth_c3 = chr(0xFF23) + chr(0xFF13)

    assert {
        "SOLD A12 M x2",
        "slod C3",
        f"C3 x {keycap_two}",
        f"{fullwidth_c3} je prends",
        "C3 dispo ?",
        "je prends A12 M et C3",
    } <= texts


def test_each_comment_appears_once() -> None:
    texts = Counter(line.case.text for line in CORPUS)

    assert [text for text, count in texts.items() if count > 1] == []


def test_a_wrong_expectation_is_reported_with_its_line(tmp_path: Path) -> None:
    corpus = tmp_path / "comments.jsonl"
    corpus.write_text(
        '{"text": "je prends B07", "kind": "match", "code": "B07", "variant": "",'
        ' "quantity": 1, "confidence": "1.00", "tag": "fr-verb"}\n'
        '{"text": "je prends B07 x2", "kind": "match", "code": "B07", "variant": "",'
        ' "quantity": 3, "confidence": "1.00", "tag": "quantity"}\n',
        encoding="utf-8",
    )

    [failure] = mismatches(load_corpus(corpus), demo_catalog(), max_quantity=DEMO_MAX_QUANTITY)

    assert failure.startswith("line 2 [quantity] 'je prends B07 x2'")
    assert "'quantity': 3" in failure
    assert "'quantity': 2" in failure


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not json",
        '{"text": "B07", "kind": "match", "code": "B07", "tag": "code-alone"}',
        '{"text": "B07", "kind": "rejected", "reason": "no_intent", "tag": "x", "extra": 1}',
        '{"text": "B07", "kind": "rejected", "reason": "unknown_code", "tag": "x"}',
        '{"text": "B07", "kind": "ambiguous", "reason": "no_intent", "candidates": [], "tag": "x"}',
        '{"text": "B07", "kind": "rejected", "reason": "no_intent", "tag": "Not A Tag"}',
        '{"text": "B07", "kind": "maybe", "reason": "no_intent", "tag": "x"}',
    ],
    ids=[
        "blank",
        "not-json",
        "missing-fields",
        "extra-field",
        "ambiguity-as-rejection",
        "rejection-as-ambiguity",
        "bad-tag",
        "unknown-kind",
    ],
)
def test_a_malformed_line_is_refused_with_its_number(tmp_path: Path, line: str) -> None:
    corpus = tmp_path / "comments.jsonl"
    corpus.write_text(
        '{"text": "B07", "kind": "rejected", "reason": "no_intent", "tag": "x"}\n' + line + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CorpusError, match="line 2"):
        load_corpus(corpus)
