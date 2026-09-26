"""The LLM evaluation on a Messages API played by ``httpx2.MockTransport``: no call leaves the host.

The model is played from the corpus itself: it answers each comment with
the case's resolution, except where a test makes it wrong.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from livecart_core.settings import LLM_MAX_RETRIES, LlmSettings
from livecart_worker.llm.evaluation import NO_KEY_MESSAGE, ambiguous_cases, run

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "llm_eval.py"
RESOLUTIONS = {case.text: case.resolution for _, case in ambiguous_cases()}


def _comment_of(request: httpx2.Request) -> str:
    """The comment a request asks about, unescaped from between its tags."""
    content: str = json.loads(request.content)["messages"][0]["content"]
    escaped = content.split("<comment>", 1)[1].rsplit("</comment>", 1)[0]
    return escaped.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _answer_from_the_corpus(text: str) -> dict[str, Any]:
    resolution = RESOLUTIONS[text]
    if resolution is None:
        return {
            "reason": "no_order",
            "product_code": None,
            "variant": None,
            "quantity": None,
            "confidence": 0.9,
        }
    return {
        "reason": "order",
        "product_code": resolution.code,
        "variant": resolution.variant or None,
        "quantity": resolution.quantity,
        "confidence": 0.9,
    }


def _api(
    answer: Callable[[str], dict[str, Any]] = _answer_from_the_corpus, *, output_tokens: int = 50
) -> AsyncAnthropic:
    async def handle(request: httpx2.Request) -> httpx2.Response:
        body = {
            "id": "msg_eval",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": json.dumps(answer(_comment_of(request)))}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 600, "output_tokens": output_tokens},
        }
        return httpx2.Response(200, json=body, headers={"request-id": "req_eval"})

    return AsyncAnthropic(
        api_key="sk-ant-test",
        max_retries=LLM_MAX_RETRIES,
        http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle)),
    )


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> LlmSettings:
    """The defaults, with no key from the environment nor from any .env."""
    for name in ("ANTHROPIC_API_KEY", "LLM_MODEL", "LLM_EFFORT", "LLM_PRICES"):
        monkeypatch.delenv(name, raising=False)
    return LlmSettings(_env_file=None)


async def test_without_a_key_it_says_so_and_exits_0(settings: LlmSettings) -> None:
    written: list[str] = []

    assert await run([], settings=settings, write=written.append) == 0
    assert written == [NO_KEY_MESSAGE]


def test_the_script_itself_exits_0_without_a_key(tmp_path: Path) -> None:
    # Run from an empty folder, so that no .env can hold a key.
    environment = {name: value for name, value in os.environ.items() if name != "ANTHROPIC_API_KEY"}

    done = subprocess.run(  # noqa: S603 - the test's own interpreter and script
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "ANTHROPIC_API_KEY is not set" in done.stdout


async def test_a_model_that_reads_as_the_corpus_decides_every_case_right(
    settings: LlmSettings,
) -> None:
    written: list[str] = []

    code = await run([], settings=settings, client=_api(), write=written.append)

    cases = len(RESOLUTIONS)
    assert code == 0
    assert written[0] == (
        f"LLM fallback evaluation: claude-opus-5, effort low, {cases} ambiguous cases of the corpus"
    )
    assert f"Decided right: {cases} of {cases} asked (100.0%); 0 not asked, the budget spent" in (
        written
    )
    assert f"Outcomes: ok {cases}" in written
    assert f"Tokens: {600 * cases} in, {50 * cases} out; largest output 50 of max_tokens 1024" in (
        written
    )
    # 600 input and 50 output tokens at $5 and $25 per million: $0.00425 a case.
    assert written[-1] == f"Cost at list price: ${Decimal('0.00425') * cases:.4f} (approximate)"


async def test_each_miss_is_listed_with_what_the_model_read(settings: LlmSettings) -> None:
    def always_two_b07(text: str) -> dict[str, Any]:
        return {
            "reason": "order",
            "product_code": "B07",
            "variant": None,
            "quantity": 2,
            "confidence": 0.9,
        }

    written: list[str] = []

    await run(
        ["--limit", "3"], settings=settings, client=_api(always_two_b07), write=written.append
    )

    misses = [line for line in written if line.startswith("  miss")]
    assert misses == [
        f"  miss  line {number} [{case.tag}] {case.text!r}: expected no order, "
        "decided B07 x2 (ok, order)"
        for number, case in ambiguous_cases()[:3]
    ]
    assert "Decided right: 0 of 3 asked (0.0%); 0 not asked, the budget spent" in written


async def test_the_run_stops_asking_once_its_budget_is_spent(settings: LlmSettings) -> None:
    written: list[str] = []

    # 10 000 output tokens at $25 per million: $0.25 a case, past $0.30 after two.
    await run(
        ["--max-cost-usd", "0.30"],
        settings=settings,
        client=_api(output_tokens=10_000),
        write=written.append,
    )

    cases = len(RESOLUTIONS)
    assert f"Decided right: 2 of 2 asked (100.0%); {cases - 2} not asked, the budget spent" in (
        written
    )


async def test_a_model_without_a_price_is_refused(settings: LlmSettings) -> None:
    written: list[str] = []

    code = await run(
        ["--model", "claude-future-9"], settings=settings, client=_api(), write=written.append
    )

    assert code == 2
    assert written == ["claude-future-9 has no price in LLM_PRICES: its cost could not be counted."]
