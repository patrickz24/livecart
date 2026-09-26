"""Evaluate the LLM fallback on the ambiguous cases of the parser's corpus, on the real API.

    uv run --locked python scripts/llm_eval.py
    uv run --locked python scripts/llm_eval.py --model claude-haiku-4-5 --limit 10

It reads ANTHROPIC_API_KEY and the LLM_* settings from the environment or
from .env, as the worker does (``LlmSettings``), and stops asking once the
run cost ``--max-cost-usd`` ($0.50 by default). Without a key it says so and
exits 0: it calls the real Anthropic API, and never runs in CI. Every change
of LLM_MODEL goes through it first (ADR 0006). What it measures and prints:
``livecart_worker.llm.evaluation``.
"""

from livecart_worker.llm.evaluation import main

if __name__ == "__main__":
    main()
