"""What the comment queue costs the compose Redis, measured, against what the settings assume.

The startup check (``Settings``) allows QUEUE_MAX_DEPTH envelopes of
QUEUE_MAX_ENVELOPE_BYTES, times 5/4 for the allocator's rounding, in half of
maxmemory. These tests measure an envelope at the bound twice: ``MEMORY
USAGE`` (the stream's own bytes) and the growth of ``used_memory`` (what
jemalloc allocated, which is what maxmemory limits), and check that the
compose Redis enforces the budget the API assumes, without eviction.
"""

import asyncio
from typing import Any, Final
from uuid import UUID

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis

from livecart_core.application.dto import IncomingComment, Platform, QueuedComment
from livecart_core.infrastructure.comment_queue import RedisCommentQueue
from livecart_core.settings import QUEUE_ENTRY_MEMORY_FACTOR, Settings
from livecart_core.testing.fakes import FAKE_CLOCK_START

pytestmark = pytest.mark.integration

ENVELOPE_BYTES: Final[int] = Settings.model_fields["queue_max_envelope_bytes"].default
ENTRIES: Final = 2_000


def _envelope(number: int, size: int) -> QueuedComment:
    """An envelope of exactly ``size`` bytes of JSON: 1000 characters, padded with escapes.

    A control character is escaped to six bytes in JSON, two more than an
    emoji, and one more letter in the author's id settles an odd remainder.
    """

    def build(escapes: int, author: str) -> QueuedComment:
        return QueuedComment(
            comment=IncomingComment(
                platform=Platform.SIMULATOR,
                external_comment_id=f"comment-{number:07d}",
                external_live_id="friday-drop",
                author_platform_user_id=author,
                text="\x01" * escapes + "😀" * (1000 - escapes),
                posted_at=FAKE_CLOCK_START,
            ),
            correlation_id=str(UUID(int=number)),
            request_id=UUID(int=number).hex,
            enqueued_at=FAKE_CLOCK_START,
        )

    missing = size - len(build(0, "buyer").model_dump_json().encode())
    envelope = build(missing // 2, "buyer" + "x" * (missing % 2))
    assert len(envelope.model_dump_json().encode()) == size
    return envelope


async def _used_memory(client: Redis) -> int:
    info: Any = await client.info("memory")
    return int(info["used_memory"])


async def test_an_envelope_at_the_bound_costs_redis_within_the_budgeted_factor(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    queue = RedisCommentQueue(redis_client, prefix=redis_key_prefix)
    envelopes = [_envelope(number, ENVELOPE_BYTES) for number in range(ENTRIES)]
    await asyncio.sleep(0.1)
    before = await _used_memory(redis_client)

    for start in range(0, ENTRIES, 200):
        await queue.enqueue(envelopes[start : start + 200])

    allocated = (await _used_memory(redis_client) - before) / ENTRIES
    stream: Any = await redis_client.memory_usage(queue.stream_key, samples=0)
    budget = ENVELOPE_BYTES * QUEUE_ENTRY_MEMORY_FACTOR
    # MEMORY USAGE counts the listpack's bytes: the envelope and about 50 more.
    assert ENVELOPE_BYTES < int(stream) / ENTRIES < ENVELOPE_BYTES + 100
    # The allocator rounds each entry up to its size class (6144 bytes here):
    # more than MEMORY USAGE shows, within the 5/4 the settings budget.
    assert int(stream) / ENTRIES < allocated <= budget


async def test_the_compose_redis_bounds_its_memory_and_never_evicts(redis_client: Redis) -> None:
    class ComposeRedis(BaseSettings):
        """REDIS_MAXMEMORY_BYTES as compose reads it: from the environment or .env."""

        model_config = SettingsConfigDict(env_file=".env", extra="ignore")

        redis_maxmemory_bytes: int = Settings.model_fields["redis_maxmemory_bytes"].default

    config: Any = await redis_client.config_get("maxmemory*")
    persistence: Any = await redis_client.config_get("append*")

    assert int(config["maxmemory"]) == ComposeRedis().redis_maxmemory_bytes
    assert config["maxmemory-policy"] == "noeviction"
    assert (persistence["appendonly"], persistence["appendfsync"]) == ("yes", "everysec")
