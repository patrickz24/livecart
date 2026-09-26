"""The evaluation of the LLM fallback on the parser's corpus, which ``scripts/llm_eval.py`` runs.

Every ambiguous case of the corpus (``livecart_core.testing.corpus``) goes
through the worker's own path: ``AnthropicIntentFallback`` behind
``LlmGuard``, one call at a time, with a budget of its own, in memory (the
tests' ``InMemoryLlmBudget``, since the run is one process), whose daily cap
is ``--max-cost-usd``: a run never spends more than that, and its last call.
The receipt is decided by the domain (``decide_from_llm``) against the demo
catalog, and the decision compared with the case's resolution: right when
both name the same order, or both none. The cases the budget stopped are
counted apart, never as right.

The report lists each miss, then the accuracy, the outcomes, the tokens,
the largest output next to ``MAX_TOKENS`` (the margin for thinking), the
latency and the cost at list price, from the usage the API reported. It
calls the real API: it needs ``ANTHROPIC_API_KEY``, and never runs in CI.
Without a key it says so and exits 0.
"""

import argparse
import asyncio
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from anthropic import AsyncAnthropic

from livecart_core.application.dto import FallbackReceipt, FallbackRequest
from livecart_core.application.llm_guard import LlmGuard
from livecart_core.domain.catalog import Catalog
from livecart_core.domain.llm_decision import decide_from_llm
from livecart_core.domain.outcomes import LlmOutcome
from livecart_core.domain.parser import Ambiguous, parse_comment
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.logging import configure_logging
from livecart_core.settings import LLM_MAX_RETRIES, LlmSettings, LogLevel
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, demo_catalog
from livecart_core.testing.corpus import AmbiguousCase, Resolution, load_corpus
from livecart_core.testing.fakes import InMemoryLlmBudget
from livecart_worker.llm.claude import MAX_TOKENS, AnthropicIntentFallback

# The budget key of the run: no session of any database.
EVAL_SESSION = UUID(int=0)
DEFAULT_MAX_COST_USD = Decimal("0.50")
NO_KEY_MESSAGE = (
    "ANTHROPIC_API_KEY is not set: nothing to evaluate. scripts/llm_eval.py calls the "
    "real Anthropic API, and never runs in CI; set the key in the environment or in .env "
    "to run it."
)


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One ambiguous case: what it should settle, the receipt, and what the domain decided."""

    line: int
    tag: str
    text: str
    expected: Resolution | None
    decided: Resolution | None
    receipt: FallbackReceipt

    @property
    def asked(self) -> bool:
        """Whether the model was asked at all: not when the run's budget was spent."""
        return self.receipt.outcome is not LlmOutcome.BUDGET_EXCEEDED

    @property
    def right(self) -> bool:
        return self.asked and self.decided == self.expected


@dataclass
class Report:
    """The results of a run, and its report as lines of text."""

    model: str
    effort: str
    results: list[CaseResult] = field(default_factory=list)
    spent_usd: Decimal = Decimal(0)

    @property
    def asked(self) -> list[CaseResult]:
        return [result for result in self.results if result.asked]

    @property
    def accuracy(self) -> float:
        asked = self.asked
        return sum(result.right for result in asked) / len(asked) if asked else 0.0

    def lines(self) -> list[str]:
        asked = self.asked
        lines = [
            f"LLM fallback evaluation: {self.model}, effort {self.effort}, "
            f"{len(self.results)} ambiguous cases of the corpus"
        ]
        lines += [
            f"  miss  line {result.line} [{result.tag}] {result.text!r}: expected "
            f"{_order(result.expected)}, decided {_order(result.decided)} "
            f"({_reading(result.receipt)})"
            for result in asked
            if not result.right
        ]
        right = sum(result.right for result in asked)
        lines.append(
            f"Decided right: {right} of {len(asked)} asked ({self.accuracy:.1%}); "
            f"{len(self.results) - len(asked)} not asked, the budget spent"
        )
        outcomes = Counter(result.receipt.outcome.value for result in self.results)
        lines.append("Outcomes: " + ", ".join(f"{name} {n}" for name, n in outcomes.most_common()))
        inputs = [result.receipt.input_tokens or 0 for result in asked]
        outputs = [result.receipt.output_tokens or 0 for result in asked]
        lines.append(
            f"Tokens: {sum(inputs)} in, {sum(outputs)} out; largest output "
            f"{max(outputs, default=0)} of max_tokens {MAX_TOKENS}"
        )
        latencies = [
            result.receipt.latency_ms for result in asked if result.receipt.latency_ms is not None
        ]
        if latencies:
            lines.append(
                f"Latency: median {statistics.median(latencies):.0f} ms, max {max(latencies)} ms"
            )
        lines.append(f"Cost at list price: ${self.spent_usd:.4f} (approximate)")
        return lines


def _order(resolution: Resolution | None) -> str:
    if resolution is None:
        return "no order"
    variant = f" {resolution.variant}" if resolution.variant else ""
    return f"{resolution.code}{variant} x{resolution.quantity}"


def _reading(receipt: FallbackReceipt) -> str:
    if receipt.proposal is None:
        return receipt.outcome.value
    return f"{receipt.outcome.value}, {receipt.proposal.reason.value}"


def ambiguous_cases() -> list[tuple[int, AmbiguousCase]]:
    """The ambiguous cases of the corpus, with their line numbers."""
    return [
        (line.number, line.case) for line in load_corpus() if isinstance(line.case, AmbiguousCase)
    ]


def _decided(receipt: FallbackReceipt, catalog: Catalog) -> Resolution | None:
    if receipt.proposal is None:
        return None
    choice = decide_from_llm(receipt.proposal, catalog, max_quantity=DEMO_MAX_QUANTITY)
    if choice is None:
        return None
    return Resolution(
        code=str(choice.product_code), variant=str(choice.variant), quantity=choice.quantity.value
    )


async def evaluate(
    guard: LlmGuard,
    cases: Sequence[tuple[int, AmbiguousCase]],
    *,
    model: str,
    effort: str,
    timeout_seconds: float,
) -> Report:
    """Ask about each case in turn, as the worker would, and compare the decisions."""
    catalog = demo_catalog()
    report = Report(model=model, effort=effort)
    for number, case in cases:
        ambiguity = parse_comment(case.text, catalog, max_quantity=DEMO_MAX_QUANTITY)
        if not isinstance(ambiguity, Ambiguous):
            raise ValueError(f"line {number} is no longer ambiguous: run the corpus test")
        receipt = await guard.resolve(
            FallbackRequest(
                session_id=EVAL_SESSION,
                text=case.text,
                ambiguity=ambiguity,
                catalog=catalog,
                timeout_seconds=timeout_seconds,
            )
        )
        report.results.append(
            CaseResult(
                number, case.tag, case.text, case.resolution, _decided(receipt, catalog), receipt
            )
        )
    report.spent_usd = guard.spent_usd
    return report


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/llm_eval.py",
        description="Run the corpus's ambiguous cases through the LLM fallback, on the real API.",
    )
    parser.add_argument("--model", help="the model to evaluate (default: LLM_MODEL)")
    parser.add_argument("--limit", type=int, help="only the first LIMIT ambiguous cases")
    parser.add_argument(
        "--max-cost-usd",
        type=Decimal,
        default=DEFAULT_MAX_COST_USD,
        help=f"stop asking once the run cost this much (default: {DEFAULT_MAX_COST_USD})",
    )
    return parser.parse_args(argv)


async def run(
    argv: Sequence[str] | None = None,
    *,
    settings: LlmSettings | None = None,
    client: AsyncAnthropic | None = None,
    write: Callable[[str], None] = print,
) -> int:
    """The script: 0 once the report is written, or at once without a key."""
    arguments = _arguments(argv)
    settings = settings if settings is not None else LlmSettings()
    model = arguments.model or settings.llm_model
    own_client = client is None
    if client is None:
        if settings.anthropic_api_key is None:
            write(NO_KEY_MESSAGE)
            return 0
        client = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=LLM_MAX_RETRIES,
            timeout=settings.llm_call_timeout_s,
        )
    if model not in settings.llm_prices:
        write(f"{model} has no price in LLM_PRICES: its cost could not be counted.")
        if own_client:
            await client.close()
        return 2
    guard = LlmGuard(
        AnthropicIntentFallback(
            client,
            model=model,
            effort=settings.llm_effort,
            refusal_fallback=settings.llm_refusal_fallback,
            call_timeout_s=settings.llm_call_timeout_s,
            max_quantity=DEMO_MAX_QUANTITY,
        ),
        # The run's own budget, in this process only: the Redis one is the workers'.
        InMemoryLlmBudget(),
        SystemClock(),
        model=model,
        prices=settings.llm_prices,
        max_calls_per_session=10_000,
        daily_budget_usd=arguments.max_cost_usd,
        slots=1,
        slot_wait=timedelta(seconds=settings.llm_slot_wait_s),
        call_budget=timedelta(seconds=settings.llm_call_budget_s),
    )
    cases = ambiguous_cases()[: arguments.limit]
    effort = "none" if settings.llm_effort is None else settings.llm_effort.value
    try:
        report = await evaluate(
            guard, cases, model=model, effort=effort, timeout_seconds=settings.llm_call_budget_s
        )
    finally:
        if own_client:
            await client.close()
    for line in report.lines():
        write(line)
    return 0


def main() -> None:
    # The report is the output: only the calls that went wrong are logged beside it.
    configure_logging(service="llm-eval", level=LogLevel.WARNING)
    raise SystemExit(asyncio.run(run()))
