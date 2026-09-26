"""The French and English comment corpus of the parser, and its loader.

``comments.jsonl`` holds one case per line: a comment, the exact result the
parser must give for it against ``builders.demo_catalog()`` with
``builders.DEMO_MAX_QUANTITY``, and a short tag. An ambiguous case also
holds its resolution: the order a careful reader takes from the comment, or
none, which the evaluation of the LLM fallback (``scripts/llm_eval.py``)
compares its decisions with. README.md, next to it, explains the format and
how to add a case.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from livecart_core.domain.catalog import Catalog
from livecart_core.domain.outcomes import AmbiguityReason, RejectionReason
from livecart_core.domain.parser import Ambiguous, Match, ParseResult, parse_comment

CORPUS_PATH: Final = Path(__file__).with_name("comments.jsonl")


class _Case(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    tag: str = Field(min_length=1, pattern=r"^[a-z0-9-]+$")

    def expected(self) -> dict[str, object]:
        """The fields a parse result is compared on: all but the text and the tag."""
        return self.model_dump(mode="json", exclude={"text", "tag"})


class MatchCase(_Case):
    kind: Literal["match"]
    code: str
    variant: str
    quantity: int
    confidence: Literal["1.00", "0.90", "0.80"]


class Resolution(BaseModel):
    """The one order an ambiguous comment carries: a product, its variant, a quantity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    variant: str
    quantity: int


class AmbiguousCase(_Case):
    kind: Literal["ambiguous"]
    reason: AmbiguityReason
    candidates: list[str]
    # What the fallback should settle, or None when the comment orders nothing
    # the catalog can take: required, so that every ambiguous case is labelled.
    resolution: Resolution | None

    def expected(self) -> dict[str, object]:
        """The parser's result only: the resolution is the fallback's to reach."""
        return self.model_dump(mode="json", exclude={"text", "tag", "resolution"})


class RejectedCase(_Case):
    kind: Literal["rejected"]
    reason: RejectionReason


type CorpusCase = Annotated[MatchCase | AmbiguousCase | RejectedCase, Field(discriminator="kind")]
_CASE: Final = TypeAdapter[CorpusCase](CorpusCase)


class CorpusError(ValueError):
    """A line of the corpus that is not a valid case."""


@dataclass(frozen=True, slots=True)
class CorpusLine:
    number: int
    case: MatchCase | AmbiguousCase | RejectedCase


def load_corpus(path: Path = CORPUS_PATH) -> list[CorpusLine]:
    """Every case of the corpus, with its line number; a blank line is an error."""
    lines = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            lines.append(CorpusLine(number, _CASE.validate_json(line)))
        except ValidationError as exc:
            raise CorpusError(f"{path.name}, line {number}: {exc}") from None
    return lines


def describe(result: ParseResult) -> dict[str, object]:
    """A parse result in the shape of a corpus case, for an exact comparison."""
    if isinstance(result, Match):
        return {
            "kind": "match",
            "code": str(result.product_code),
            "variant": str(result.variant),
            "quantity": result.quantity.value,
            "confidence": str(result.confidence.value),
        }
    if isinstance(result, Ambiguous):
        return {
            "kind": "ambiguous",
            "reason": str(result.reason),
            "candidates": [str(code) for code in result.candidates],
        }
    return {"kind": "rejected", "reason": str(result.reason)}


def mismatches(lines: Iterable[CorpusLine], catalog: Catalog, *, max_quantity: int) -> list[str]:
    """One readable report per case whose parse result differs from the expected one."""
    reports = []
    for line in lines:
        case = line.case
        actual = describe(parse_comment(case.text, catalog, max_quantity=max_quantity))
        if actual != case.expected():
            reports.append(
                f"line {line.number} [{case.tag}] {case.text!r}\n"
                f"    expected {case.expected()}\n"
                f"    actual   {actual}"
            )
    return reports
