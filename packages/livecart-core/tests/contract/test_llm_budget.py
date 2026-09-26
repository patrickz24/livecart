"""``LlmBudget`` on every backend: the in-memory fake, then Redis.

The day of a spend is the UTC date of the ``now`` given, so the scenarios
move between days by passing another ``now``, on both backends alike.
"""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import LlmBudgetUsage
from livecart_core.application.ports import LlmBudget

SESSION = UUID("0192f0c1-0000-7000-8000-000000000001")
OTHER_SESSION = UUID("0192f0c1-0000-7000-8000-000000000002")
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


async def test_a_new_session_on_a_new_day_has_spent_nothing(llm_budget: LlmBudget) -> None:
    assert await llm_budget.usage(SESSION, now=NOW) == LlmBudgetUsage(0, Decimal(0))


async def test_reading_the_usage_counts_nothing(llm_budget: LlmBudget) -> None:
    await llm_budget.usage(SESSION, now=NOW)

    assert (await llm_budget.usage(SESSION, now=NOW)).session_calls == 0


async def test_each_call_counts_against_its_own_session(llm_budget: LlmBudget) -> None:
    first = await llm_budget.count_call(SESSION, now=NOW)
    second = await llm_budget.count_call(SESSION, now=NOW)
    other = await llm_budget.count_call(OTHER_SESSION, now=NOW)

    assert (first.session_calls, second.session_calls, other.session_calls) == (1, 2, 1)
    assert (await llm_budget.usage(SESSION, now=NOW)).session_calls == 2


async def test_concurrent_calls_each_get_a_count_of_their_own(llm_budget: LlmBudget) -> None:
    usages = await asyncio.gather(*(llm_budget.count_call(SESSION, now=NOW) for _ in range(40)))

    assert sorted(usage.session_calls for usage in usages) == list(range(1, 41))


async def test_the_spend_adds_up_over_the_day_for_every_session(llm_budget: LlmBudget) -> None:
    assert await llm_budget.add_spend(Decimal("0.004"), now=NOW) == Decimal("0.004")
    assert await llm_budget.add_spend(Decimal("0.0125"), now=NOW) == Decimal("0.0165")

    assert (await llm_budget.usage(SESSION, now=NOW)).spent_today_usd == Decimal("0.0165")
    assert (await llm_budget.count_call(OTHER_SESSION, now=NOW)).spent_today_usd == Decimal(
        "0.0165"
    )


async def test_each_cost_is_rounded_up_to_a_micro_dollar(llm_budget: LlmBudget) -> None:
    spent = await llm_budget.add_spend(Decimal("0.0000001"), now=NOW)
    spent = await llm_budget.add_spend(Decimal("0.0000021"), now=NOW)

    assert spent == Decimal("0.000004")


async def test_a_new_utc_day_starts_from_nothing(llm_budget: LlmBudget) -> None:
    before_midnight = datetime(2026, 9, 26, 23, 59, tzinfo=UTC)
    await llm_budget.add_spend(Decimal("0.5"), now=before_midnight)

    after_midnight = before_midnight + timedelta(minutes=2)
    # 01:30 in Paris is still the 26th in UTC: the same day's budget.
    paris = datetime(2026, 9, 27, 1, 30, tzinfo=timezone(timedelta(hours=2)))

    assert (await llm_budget.usage(SESSION, now=after_midnight)).spent_today_usd == 0
    assert (await llm_budget.usage(SESSION, now=paris)).spent_today_usd == Decimal("0.5")


async def test_the_calls_of_a_session_do_not_start_again_with_the_day(
    llm_budget: LlmBudget,
) -> None:
    await llm_budget.count_call(SESSION, now=NOW)

    tomorrow = await llm_budget.usage(SESSION, now=NOW + timedelta(days=1))

    assert tomorrow.session_calls == 1


async def test_a_negative_cost_is_refused(llm_budget: LlmBudget) -> None:
    with pytest.raises(ValueError, match="never negative"):
        await llm_budget.add_spend(Decimal("-0.01"), now=NOW)
