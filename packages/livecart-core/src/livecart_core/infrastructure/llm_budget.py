"""``LlmBudget`` on Redis: the LLM's calls per session and its dollars per UTC day.

Two kinds of counters, shared by every worker:

- ``{prefix}:llm:calls:{session_id}``: the calls of one live session, one
  ``INCR`` each, which expires two days after the session's first call;
- ``{prefix}:llm:spend:{YYYY-MM-DD}``: the day's spend in whole
  micro-dollars, one ``INCRBY`` per call, each cost rounded up, which
  expires two days after the day's first call.

Each method is one Lua script, loaded once per process with
``register_script``, so that a count and its expiry, or a count and the
spend read with it, are one atomic step. ``EXPIRE ... NX`` sets an expiry
only on a counter that has none: later calls never push it back, and a
counter left without one (by hand, say) gets one at its next call. It needs
Redis 7.0, as the plan's other commands do.

Any Redis error raises ``ServiceUnavailable``.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from livecart_core.application.dto import MICRO_USD, LlmBudgetUsage, to_micro_usd
from livecart_core.application.ports import ServiceUnavailable

# Longer than any live, and than a day: a counter outlives what it counts.
COUNTER_TTL_SECONDS: Final = 2 * 86_400

USAGE_SCRIPT: Final = """
local calls
if ARGV[1] == "1" then
  calls = redis.call("INCR", KEYS[1])
  redis.call("EXPIRE", KEYS[1], ARGV[2], "NX")
else
  calls = tonumber(redis.call("GET", KEYS[1]) or "0")
end
return {calls, tonumber(redis.call("GET", KEYS[2]) or "0")}
"""

SPEND_SCRIPT: Final = """
local spent = redis.call("INCRBY", KEYS[1], ARGV[1])
redis.call("EXPIRE", KEYS[1], ARGV[2], "NX")
return spent
"""


class RedisLlmBudget:
    """The LLM budget's counters in Redis; see the module docstring."""

    def __init__(self, client: Redis, *, prefix: str) -> None:
        self._prefix = prefix
        self._usage = client.register_script(USAGE_SCRIPT)
        self._spend = client.register_script(SPEND_SCRIPT)

    def calls_key(self, session_id: UUID) -> str:
        return f"{self._prefix}:llm:calls:{session_id}"

    def spend_key(self, now: datetime) -> str:
        return f"{self._prefix}:llm:spend:{now.astimezone(UTC).date().isoformat()}"

    async def usage(self, session_id: UUID, *, now: datetime) -> LlmBudgetUsage:
        return await self._read(session_id, now, count=False)

    async def count_call(self, session_id: UUID, *, now: datetime) -> LlmBudgetUsage:
        return await self._read(session_id, now, count=True)

    async def add_spend(self, cost_usd: Decimal, *, now: datetime) -> Decimal:
        if cost_usd < 0:
            raise ValueError(f"a cost is never negative, not {cost_usd}")
        try:
            spent = await self._spend(
                keys=[self.spend_key(now)], args=[to_micro_usd(cost_usd), COUNTER_TTL_SECONDS]
            )
        except RedisError as error:
            raise ServiceUnavailable(f"llm budget unavailable: {type(error).__name__}") from error
        return int(spent) * MICRO_USD

    async def _read(self, session_id: UUID, now: datetime, *, count: bool) -> LlmBudgetUsage:
        try:
            calls, spent = await self._usage(
                keys=[self.calls_key(session_id), self.spend_key(now)],
                args=[1 if count else 0, COUNTER_TTL_SECONDS],
            )
        except RedisError as error:
            raise ServiceUnavailable(f"llm budget unavailable: {type(error).__name__}") from error
        return LlmBudgetUsage(int(calls), int(spent) * MICRO_USD)
