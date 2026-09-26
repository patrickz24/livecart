"""``LlmGuard``: the budget, the bounded slot, the time left and the spend, over the fakes.

The slot is waited for on the real event loop, with waits of a fraction of a
second; the budget is ``InMemoryLlmBudget``, whose scenarios Redis shares.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    FallbackReceipt,
    FallbackRequest,
    LlmBudgetUsage,
    LlmPrice,
)
from livecart_core.application.llm_guard import LlmGuard
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.domain.outcomes import LlmOutcome
from livecart_core.domain.parser import Ambiguous, parse_comment
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, demo_catalog
from livecart_core.testing.fakes import (
    FakeClock,
    FakeIntentFallback,
    InMemoryLlmBudget,
    ok_receipt,
)

SESSION = UUID("0192f0c1-0000-7000-8000-000000000001")
OTHER_SESSION = UUID("0192f0c1-0000-7000-8000-000000000002")
MODEL = "claude-opus-5"
PRICES = {
    MODEL: LlmPrice(input_per_mtok=Decimal(5), output_per_mtok=Decimal(25)),
    "claude-haiku-4-5": LlmPrice(input_per_mtok=Decimal(1), output_per_mtok=Decimal(5)),
}
# 1000 input and 200 output tokens at $5 and $25 per million: $0.005 + $0.005.
METERED = replace(ok_receipt("B07", quantity=2), model=MODEL, input_tokens=1000, output_tokens=200)


def _request(*, timeout_seconds: float = 25.0, session_id: UUID = SESSION) -> FallbackRequest:
    ambiguity = parse_comment("je prends deux B07", demo_catalog(), max_quantity=DEMO_MAX_QUANTITY)
    assert isinstance(ambiguity, Ambiguous)
    return FallbackRequest(
        session_id=session_id,
        text="je prends deux B07",
        ambiguity=ambiguity,
        catalog=demo_catalog(),
        timeout_seconds=timeout_seconds,
    )


class _CountingFallback(FakeIntentFallback):
    """The fake, noting how many calls run at once."""

    running = 0
    most_running = 0

    async def resolve(self, request: FallbackRequest) -> FallbackReceipt:
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            return await super().resolve(request)
        finally:
            self.running -= 1


class _CountedElsewhere(InMemoryLlmBudget):
    """Another worker counts a call of the session between this read and this count."""

    async def count_call(self, session_id: UUID, *, now: datetime) -> LlmBudgetUsage:
        await super().count_call(session_id, now=now)
        return await super().count_call(session_id, now=now)


def _guard(
    fallback: FakeIntentFallback,
    budget: InMemoryLlmBudget | None = None,
    *,
    max_calls: int = 200,
    daily_budget: str = "1.00",
    slots: int = 4,
    slot_wait_s: float = 7.0,
    call_budget_s: float = 18.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> LlmGuard:
    return LlmGuard(
        fallback,
        budget if budget is not None else InMemoryLlmBudget(),
        FakeClock(),
        model=MODEL,
        prices=PRICES,
        max_calls_per_session=max_calls,
        daily_budget_usd=Decimal(daily_budget),
        slots=slots,
        slot_wait=timedelta(seconds=slot_wait_s),
        call_budget=timedelta(seconds=call_budget_s),
        monotonic=monotonic,
    )


async def _usage(budget: InMemoryLlmBudget, session_id: UUID = SESSION) -> LlmBudgetUsage:
    return await budget.usage(session_id, now=FakeClock().now())


# --- A call within the budget ----------------------------------------------------------


async def test_a_call_within_the_budget_is_counted_and_its_cost_spent() -> None:
    budget = InMemoryLlmBudget()
    guard = _guard(FakeIntentFallback([METERED]), budget)

    receipt = await guard.resolve(_request())

    assert receipt == METERED
    assert await _usage(budget) == LlmBudgetUsage(1, Decimal("0.01"))
    assert guard.spent_usd == Decimal("0.01")
    assert guard.outcomes[LlmOutcome.OK] == 1


async def test_every_outcome_is_counted_from_zero() -> None:
    guard = _guard(FakeIntentFallback())

    assert dict(guard.outcomes) == dict.fromkeys(LlmOutcome, 0)


async def test_the_fallback_gets_the_time_left_at_most_the_call_budget() -> None:
    fallback = FakeIntentFallback([METERED, METERED])
    guard = _guard(fallback, call_budget_s=18.0)

    await guard.resolve(_request(timeout_seconds=25.0))
    await guard.resolve(_request(timeout_seconds=10.0))

    long, short = fallback.requests
    assert long.timeout_seconds == 18.0
    assert 9.9 < short.timeout_seconds <= 10.0
    # Nothing else of the request changes on its way through.
    assert replace(long, timeout_seconds=25.0) == _request()


def test_a_call_is_priced_by_the_model_that_answered() -> None:
    guard = _guard(FakeIntentFallback())

    assert guard.cost_of(METERED) == Decimal("0.01")
    assert guard.cost_of(replace(METERED, model="claude-haiku-4-5")) == Decimal("0.002")
    # A model the table lacks costs what the model asked costs.
    assert guard.cost_of(replace(METERED, model="claude-unknown-1")) == Decimal("0.01")
    assert guard.cost_of(FallbackReceipt(LlmOutcome.TIMEOUT, model=MODEL)) == 0


async def test_a_receipt_without_tokens_spends_nothing() -> None:
    budget = InMemoryLlmBudget()
    guard = _guard(FakeIntentFallback([FallbackReceipt(LlmOutcome.ERROR, model=MODEL)]), budget)

    receipt = await guard.resolve(_request())

    assert receipt.outcome is LlmOutcome.ERROR
    assert await _usage(budget) == LlmBudgetUsage(1, Decimal(0))
    assert guard.outcomes[LlmOutcome.ERROR] == 1


# --- Over budget -----------------------------------------------------------------------


async def test_a_session_at_its_cap_gets_no_more_calls_and_others_still_do() -> None:
    fallback = FakeIntentFallback([METERED, METERED, METERED])
    guard = _guard(fallback, max_calls=2)

    outcomes = [(await guard.resolve(_request())).outcome for _ in range(3)]
    other = await guard.resolve(_request(session_id=OTHER_SESSION))

    assert outcomes == [LlmOutcome.OK, LlmOutcome.OK, LlmOutcome.BUDGET_EXCEEDED]
    assert other.outcome is LlmOutcome.OK
    assert len(fallback.requests) == 3


async def test_a_day_whose_budget_is_spent_gets_no_more_calls() -> None:
    budget = InMemoryLlmBudget()
    await budget.add_spend(Decimal("1.00"), now=FakeClock().now())
    fallback = FakeIntentFallback()
    guard = _guard(fallback, budget)

    receipt = await guard.resolve(_request())

    assert receipt == FallbackReceipt(LlmOutcome.BUDGET_EXCEEDED)
    assert fallback.requests == []
    assert guard.outcomes[LlmOutcome.BUDGET_EXCEEDED] == 1


async def test_a_cap_reached_by_another_worker_meanwhile_is_checked_after_the_count() -> None:
    fallback = FakeIntentFallback()
    guard = _guard(fallback, _CountedElsewhere(), max_calls=1)

    receipt = await guard.resolve(_request())

    assert receipt.outcome is LlmOutcome.BUDGET_EXCEEDED
    assert fallback.requests == []


async def test_the_calls_in_flight_may_spend_past_the_budget() -> None:
    # The spend is known after a call: the one that crosses the budget stands.
    budget = InMemoryLlmBudget()
    guard = _guard(FakeIntentFallback([METERED, METERED]), budget, daily_budget="0.015")

    first = await guard.resolve(_request())
    second = await guard.resolve(_request())
    third = await guard.resolve(_request())

    assert [first.outcome, second.outcome] == [LlmOutcome.OK, LlmOutcome.OK]
    assert third.outcome is LlmOutcome.BUDGET_EXCEEDED
    assert (await _usage(budget)).spent_today_usd == Decimal("0.02")


async def test_a_burst_goes_past_the_budget_by_one_call_per_slot_at_most() -> None:
    # The bound ADR 0006 states: whatever the burst, the calls that pass the
    # check before any cost is known are the slots' worth, no more.
    budget = InMemoryLlmBudget()
    fallback = _CountingFallback([METERED] * 16, delay_seconds=0.05)
    guard = _guard(fallback, budget, daily_budget="0.005", slots=4)

    receipts = await asyncio.gather(
        *(guard.resolve(_request(session_id=UUID(int=n % 2 + 1))) for n in range(16))
    )

    outcomes = [receipt.outcome for receipt in receipts]
    assert outcomes.count(LlmOutcome.OK) == 4
    assert outcomes.count(LlmOutcome.BUDGET_EXCEEDED) == 12
    assert len(fallback.requests) == fallback.most_running == 4
    # Four calls of $0.01 against a budget of half a call.
    assert (await _usage(budget)).spent_today_usd == Decimal("0.04")


# --- The slot --------------------------------------------------------------------------


async def test_no_more_calls_run_at_once_than_the_slots() -> None:
    fallback = _CountingFallback([METERED] * 6, delay_seconds=0.05)
    guard = _guard(fallback, slots=2)

    receipts = await asyncio.gather(*(guard.resolve(_request()) for _ in range(6)))

    assert {receipt.outcome for receipt in receipts} == {LlmOutcome.OK}
    assert fallback.most_running == 2


async def test_a_call_waits_for_a_slot_at_most_the_slot_wait_then_is_busy() -> None:
    budget = InMemoryLlmBudget()
    guard = _guard(
        FakeIntentFallback([METERED], delay_seconds=0.4), budget, slots=1, slot_wait_s=0.1
    )

    async def second_call() -> tuple[FallbackReceipt, float]:
        await asyncio.sleep(0.02)
        started = time.monotonic()
        receipt = await guard.resolve(_request())
        return receipt, time.monotonic() - started

    first, (second, waited) = await asyncio.gather(guard.resolve(_request()), second_call())

    assert (first.outcome, second) == (LlmOutcome.OK, FallbackReceipt(LlmOutcome.LLM_BUSY))
    assert 0.09 < waited < 0.38
    # The busy one made no call: the session counts one.
    assert (await _usage(budget)).session_calls == 1
    assert guard.outcomes[LlmOutcome.LLM_BUSY] == 1


async def test_the_slot_wait_never_outlasts_the_time_the_request_has() -> None:
    guard = _guard(FakeIntentFallback([METERED], delay_seconds=0.4), slots=1, slot_wait_s=7.0)
    started = time.monotonic()

    first, second = await asyncio.gather(
        guard.resolve(_request()), guard.resolve(_request(timeout_seconds=0.1))
    )

    assert (first.outcome, second.outcome) == (LlmOutcome.OK, LlmOutcome.LLM_BUSY)
    assert time.monotonic() - started < 1.0


async def test_a_call_that_waited_gets_what_is_left_after_its_wait() -> None:
    fallback = FakeIntentFallback([METERED, METERED], delay_seconds=0.2)
    guard = _guard(fallback, slots=1, slot_wait_s=1.0)

    await asyncio.gather(guard.resolve(_request(timeout_seconds=5.0)), _second(guard))

    first, second = fallback.requests
    assert first.timeout_seconds == pytest.approx(5.0, abs=0.05)
    assert 4.0 < second.timeout_seconds < 4.85


async def _second(guard: LlmGuard) -> FallbackReceipt:
    await asyncio.sleep(0)
    return await guard.resolve(_request(timeout_seconds=5.0))


async def test_no_time_left_after_the_wait_is_a_timeout_without_a_call() -> None:
    # The clock jumps past the request's 25 s between the slot and the call.
    ticks = iter([0.0, 30.0])
    fallback = FakeIntentFallback()
    guard = _guard(fallback, monotonic=lambda: next(ticks))

    receipt = await guard.resolve(_request())

    assert receipt == FallbackReceipt(LlmOutcome.TIMEOUT)
    assert fallback.requests == []


async def test_the_slot_is_given_back_whatever_the_call_did() -> None:
    # No receipt scripted: the fake fails the first call.
    fallback = FakeIntentFallback()
    guard = _guard(fallback, slots=1, slot_wait_s=0.1)
    with pytest.raises(AssertionError):
        await guard.resolve(_request())
    fallback.script(METERED)

    assert (await guard.resolve(_request())).outcome is LlmOutcome.OK


# --- The budget's own store ------------------------------------------------------------


async def test_a_budget_that_cannot_be_read_fails_the_call_before_any_slot() -> None:
    budget = InMemoryLlmBudget()
    budget.fail_next()
    fallback = FakeIntentFallback()

    with pytest.raises(ServiceUnavailable):
        await _guard(fallback, budget).resolve(_request())
    assert fallback.requests == []


async def test_a_call_that_cannot_be_counted_fails_and_gives_its_slot_back() -> None:
    class _FailingCount(InMemoryLlmBudget):
        async def count_call(self, session_id: UUID, *, now: datetime) -> LlmBudgetUsage:
            raise ServiceUnavailable("llm budget unavailable: scripted")

    guard = _guard(FakeIntentFallback([METERED]), _FailingCount(), slots=1, slot_wait_s=0.1)

    for _ in range(2):
        with pytest.raises(ServiceUnavailable):
            await guard.resolve(_request())


async def test_a_spend_the_budget_fails_to_record_is_counted_and_the_receipt_kept() -> None:
    class _FailingSpend(InMemoryLlmBudget):
        async def add_spend(self, cost_usd: Decimal, *, now: datetime) -> Decimal:
            raise ServiceUnavailable("llm budget unavailable: scripted")

    guard = _guard(FakeIntentFallback([METERED]), _FailingSpend())

    receipt = await guard.resolve(_request())

    assert receipt == METERED
    assert (guard.unrecorded_spend_total, guard.spent_usd) == (1, Decimal("0.01"))


# --- Construction ----------------------------------------------------------------------


def _build(
    *,
    model: str = MODEL,
    max_calls: int = 200,
    daily_budget: Decimal = Decimal("1.00"),
    slots: int = 4,
    slot_wait: timedelta = timedelta(seconds=7),
    call_budget: timedelta = timedelta(seconds=18),
) -> LlmGuard:
    return LlmGuard(
        FakeIntentFallback(),
        InMemoryLlmBudget(),
        FakeClock(),
        model=model,
        prices=PRICES,
        max_calls_per_session=max_calls,
        daily_budget_usd=daily_budget,
        slots=slots,
        slot_wait=slot_wait,
        call_budget=call_budget,
    )


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: _build(model="claude-unpriced-1"), "no price"),
        (lambda: _build(max_calls=0), "calls per session"),
        (lambda: _build(daily_budget=Decimal(0)), "daily budget"),
        (lambda: _build(slots=0), "slots"),
        (lambda: _build(slot_wait=timedelta(0)), "slot wait"),
        (lambda: _build(call_budget=timedelta(0)), "call budget"),
    ],
)
def test_nonsensical_settings_are_refused(build: Callable[[], LlmGuard], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()
