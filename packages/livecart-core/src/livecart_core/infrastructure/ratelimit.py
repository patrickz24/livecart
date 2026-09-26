"""``RateLimiter`` on Redis: fixed windows counted by one Lua script.

Each counter is the key ``{prefix}:rl:{rule}:{subject}``, holding its hits.
The script runs atomically on the server, so the hits of every API process
count together and no two requests can both see the last free place:

- a hit is ``INCR``, then ``PEXPIRE ... NX``: only the first hit of a window
  sets its end, so later hits never extend it, and a key left without an
  expiry (by hand, say) gets one at its next hit;
- a peek is ``GET``, counting nothing;
- both return the hits and ``PTTL``, from which Retry-After is computed.

The script is loaded once per process with ``register_script``: redis-py
sends its SHA-1 (``EVALSHA``) and loads it again after a ``SCRIPT FLUSH`` or a
restart. ``PEXPIRE ... NX`` needs Redis 7.0, as the plan's other commands do.

Any Redis error raises ``ServiceUnavailable``; whether the request goes on is
the rule's choice (``RateLimitRule.fail_closed``).
"""

import math
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from livecart_core.application.dto import RateLimitKey, WindowCount
from livecart_core.application.ports import ServiceUnavailable

FIXED_WINDOW_SCRIPT: Final = """
local hits
if ARGV[2] == "1" then
  hits = redis.call("INCR", KEYS[1])
  redis.call("PEXPIRE", KEYS[1], ARGV[1], "NX")
else
  hits = tonumber(redis.call("GET", KEYS[1]) or "0")
end
return {hits, redis.call("PTTL", KEYS[1])}
"""


def _whole_seconds(milliseconds: int) -> int:
    """PTTL in whole seconds, rounded up; 0 when the key is gone (-2) or never expires (-1)."""
    return math.ceil(milliseconds / 1000) if milliseconds > 0 else 0


class RedisRateLimiter:
    """Fixed-window counters in Redis; see the module docstring."""

    def __init__(self, client: Redis, *, prefix: str) -> None:
        self._prefix = prefix
        self._script = client.register_script(FIXED_WINDOW_SCRIPT)

    def key_name(self, key: RateLimitKey) -> str:
        return f"{self._prefix}:rl:{key.rule}:{key.subject}"

    async def hit(self, key: RateLimitKey, *, window_seconds: int) -> WindowCount:
        if window_seconds < 1:
            raise ValueError(f"window_seconds must be positive, not {window_seconds}")
        return await self._run(key, window_ms=window_seconds * 1000, count=True)

    async def peek(self, key: RateLimitKey) -> WindowCount:
        return await self._run(key, window_ms=0, count=False)

    async def _run(self, key: RateLimitKey, *, window_ms: int, count: bool) -> WindowCount:
        try:
            hits, ttl_ms = await self._script(
                keys=[self.key_name(key)], args=[window_ms, 1 if count else 0]
            )
        except RedisError as error:
            raise ServiceUnavailable(f"rate limiter unavailable: {type(error).__name__}") from error
        return WindowCount(int(hits), _whole_seconds(int(ttl_ms)))
