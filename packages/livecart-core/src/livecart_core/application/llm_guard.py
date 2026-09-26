"""``LlmGuard``: the cost guard and the bounded slot in front of the LLM fallback.

It is an ``IntentFallback`` itself, wrapped around the real one, and every
ambiguous comment that reaches the model goes through it, in this order:

1. **The budget, read without counting** (``LlmBudget.usage``): the
   session's calls below ``LLM_MAX_CALLS_PER_SESSION`` (200) and the day's
   spend below ``LLM_DAILY_BUDGET_USD`` ($1.00), or ``budget_exceeded`` at
   once, without waiting for a slot.
2. **A slot**, one of ``slots`` (4 per worker, an ``asyncio.Semaphore``),
   waited for at most ``LLM_SLOT_WAIT_S`` (7 s), and never longer than the
   time the request has left: beyond, ``llm_busy``. A burst of ambiguous
   comments therefore never piles up behind the model until the message
   deadline: the excess is settled at once as unresolved, and its deliveries
   are acknowledged rather than timed out and redelivered.
3. **The call, counted** against its session (``LlmBudget.count_call``) and
   checked again, since the other workers count too; then the fallback, with
   the time left after the wait, ``LLM_CALL_BUDGET_S`` (18 s) at most.
4. **Its cost**, from the tokens the provider reported and the list price of
   the model that answered (``LlmPrice``), added to the day's spend.

The day's spend is checked before a call and known only after it, so the
calls in flight when it is reached may go past it: one per slot of each
worker at most, a few cents with the defaults. The prices are list prices,
per million input and output tokens: the spend approximates the bill.

The guard counts in plain numbers, which the worker's metrics read at scrape
time: the receipts by outcome, the dollars spent, and the costs the budget
failed to record.
"""

import asyncio
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from livecart_core.application.dto import FallbackReceipt, FallbackRequest, LlmBudgetUsage, LlmPrice
from livecart_core.application.ports import Clock, IntentFallback, LlmBudget, ServiceUnavailable
from livecart_core.domain.outcomes import LlmOutcome


class LlmGuard:
    """The budget and the slot of the LLM fallback; see the module docstring."""

    def __init__(
        self,
        fallback: IntentFallback,
        budget: LlmBudget,
        clock: Clock,
        *,
        model: str,
        prices: Mapping[str, LlmPrice],
        max_calls_per_session: int,
        daily_budget_usd: Decimal,
        slots: int,
        slot_wait: timedelta,
        call_budget: timedelta,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if model not in prices:
            raise ValueError(f"no price for the model {model!r}")
        if max_calls_per_session < 1 or not daily_budget_usd > 0 or slots < 1:
            raise ValueError("a guard needs calls per session, a daily budget and slots")
        if slot_wait <= timedelta(0) or call_budget <= timedelta(0):
            raise ValueError("a guard needs a slot wait and a call budget")
        self._fallback = fallback
        self._budget = budget
        self._clock = clock
        self._model = model
        self._prices = prices
        self._max_calls = max_calls_per_session
        self._daily_budget = daily_budget_usd
        self._slots = asyncio.Semaphore(slots)
        self._slot_wait_s = slot_wait.total_seconds()
        self._call_budget_s = call_budget.total_seconds()
        self._monotonic = monotonic
        # Every outcome from the start, at zero, for the metrics.
        self.outcomes: Counter[LlmOutcome] = Counter(dict.fromkeys(LlmOutcome, 0))
        self.spent_usd = Decimal(0)
        self.unrecorded_spend_total = 0

    async def resolve(self, request: FallbackRequest) -> FallbackReceipt:
        receipt = await self._guarded(request)
        self.outcomes[receipt.outcome] += 1
        return receipt

    def cost_of(self, receipt: FallbackReceipt) -> Decimal:
        """What a receipt cost, at the list price of the model that answered, or the one asked."""
        if receipt.input_tokens is None and receipt.output_tokens is None:
            return Decimal(0)
        price = self._prices.get(receipt.model or self._model, self._prices[self._model])
        return price.cost(receipt.input_tokens or 0, receipt.output_tokens or 0)

    def _over(self, usage: LlmBudgetUsage, *, counted: bool) -> bool:
        """Past a cap: the session's calls (this one included once counted), or the day's spend."""
        calls_allowed = self._max_calls if counted else self._max_calls - 1
        return usage.session_calls > calls_allowed or usage.spent_today_usd >= self._daily_budget

    async def _guarded(self, request: FallbackRequest) -> FallbackReceipt:
        usage = await self._budget.usage(request.session_id, now=self._clock.now())
        if self._over(usage, counted=False):
            return FallbackReceipt(LlmOutcome.BUDGET_EXCEEDED)
        started = self._monotonic()
        try:
            async with asyncio.timeout(min(self._slot_wait_s, request.timeout_seconds)):
                await self._slots.acquire()
        except TimeoutError:
            return FallbackReceipt(LlmOutcome.LLM_BUSY)
        try:
            usage = await self._budget.count_call(request.session_id, now=self._clock.now())
            if self._over(usage, counted=True):
                return FallbackReceipt(LlmOutcome.BUDGET_EXCEEDED)
            left = request.timeout_seconds - (self._monotonic() - started)
            if left <= 0:
                return FallbackReceipt(LlmOutcome.TIMEOUT)
            receipt = await self._fallback.resolve(
                replace(request, timeout_seconds=min(self._call_budget_s, left))
            )
        finally:
            self._slots.release()
        await self._spend(self.cost_of(receipt))
        return receipt

    async def _spend(self, cost: Decimal) -> None:
        if not cost > 0:
            return
        self.spent_usd += cost
        try:
            await self._budget.add_spend(cost, now=self._clock.now())
        except ServiceUnavailable:
            # The call is made and its receipt is next: raising now would
            # redeliver the comment and pay for a second call. The day's spend
            # misses this one, and the metrics say so.
            self.unrecorded_spend_total += 1
