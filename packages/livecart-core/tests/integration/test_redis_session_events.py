"""What only Redis shows of the session streams: their time to live, their trim, their failures.

The behaviour both backends share is in tests/contract/test_session_events.py.
"""

import time
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import pytest
import structlog
from pydantic import SecretStr
from redis.asyncio import Redis

from livecart_core.application.dto import SessionEvent, SessionEventType
from livecart_core.application.ports import START_CURSOR, ServiceUnavailable
from livecart_core.infrastructure.events import (
    ENDED_STREAM_TTL,
    LIVE_STREAM_TTL,
    RedisSessionEvents,
    ends_session,
)
from livecart_core.infrastructure.redis_client import create_redis_client
from livecart_core.testing.fakes import FAKE_CLOCK_START

pytestmark = pytest.mark.integration

SESSION = UUID(int=7)


def _event(
    event_type: SessionEventType = SessionEventType.STOCK_UPDATED, **data: str
) -> SessionEvent:
    return SessionEvent(
        type=event_type, session_id=SESSION, occurred_at=FAKE_CLOCK_START, data=dict(data)
    )


ENDED = _event(SessionEventType.SESSION_UPDATED, status="ended")


async def _ttl(client: Redis, events: RedisSessionEvents) -> int:
    ttl: int = await client.ttl(events.stream_key(SESSION))
    return ttl


async def test_a_stream_lives_a_day_after_its_last_event(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)

    await events.publish(_event())

    live = int(LIVE_STREAM_TTL.total_seconds())
    assert live - 5 <= await _ttl(redis_client, events) <= live
    assert events.stream_key(SESSION) == f"{redis_key_prefix}:events:session:{SESSION}"


async def test_once_its_session_ended_a_stream_lives_an_hour_after_each_event(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)
    ended = int(ENDED_STREAM_TTL.total_seconds())
    await events.publish(_event())

    await events.publish(ENDED)
    assert ended - 5 <= await _ttl(redis_client, events) <= ended

    # An order expiring after the end does not bring the day back.
    await redis_client.expire(events.stream_key(SESSION), 600)
    await events.publish(_event(SessionEventType.ORDER_UPDATED, status="expired"))
    assert ended - 5 <= await _ttl(redis_client, events) <= ended


def test_only_an_ended_session_update_ends_a_stream() -> None:
    assert ends_session(ENDED)
    assert not ends_session(_event(SessionEventType.SESSION_UPDATED, status="live"))
    assert not ends_session(_event(SessionEventType.ORDER_UPDATED, status="ended"))


async def test_the_retention_trims_the_events_older_than_15_minutes(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix, approximate_trim=False)
    key = events.stream_key(SESSION)
    now_ms = int(time.time() * 1000)
    old_payload = _event(product_id="old").model_dump_json()
    recent_payload = _event(product_id="recent").model_dump_json()
    await redis_client.xadd(key, {"event": old_payload}, id=f"{now_ms - 20 * 60_000}-0")
    await redis_client.xadd(key, {"event": recent_payload}, id=f"{now_ms - 10 * 60_000}-0")

    await events.publish(_event(product_id="new"))

    read = await events.read(SESSION, after=START_CURSOR, max_events=10, block_seconds=0)
    assert [item.event.data["product_id"] for item in read] == ["recent", "new"]


async def test_a_malformed_entry_is_skipped_and_logged(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)
    await redis_client.xadd(events.stream_key(SESSION), {"event": "{not json"})
    await events.publish(_event())

    with structlog.testing.capture_logs() as logs:
        read = await events.read(SESSION, after=START_CURSOR, max_events=10, block_seconds=0)

    assert [item.event for item in read] == [_event()]
    assert [log["event"] for log in logs] == ["session_event_malformed"]


@pytest.fixture
async def unreachable() -> AsyncIterator[RedisSessionEvents]:
    client = create_redis_client(SecretStr("redis://127.0.0.1:1/0"))
    try:
        yield RedisSessionEvents(client, prefix="unreachable")
    finally:
        await client.aclose()


async def test_a_failed_publication_is_logged_then_raised(unreachable: RedisSessionEvents) -> None:
    with structlog.testing.capture_logs() as logs, pytest.raises(ServiceUnavailable):
        await unreachable.publish(_event())

    [log] = logs
    assert log["event"] == "session_event_unpublished"
    assert log["event_type"] == "stock.updated"
    assert log["session_id"] == str(SESSION)
    assert log["log_level"] == "warning"


async def test_every_read_of_an_unreachable_stream_is_unavailable(
    unreachable: RedisSessionEvents,
) -> None:
    with pytest.raises(ServiceUnavailable, match="cursor"):
        await unreachable.cursor(SESSION)
    with pytest.raises(ServiceUnavailable, match="read"):
        await unreachable.read(SESSION, after=START_CURSOR, max_events=1, block_seconds=0)
    with pytest.raises(ServiceUnavailable, match="has gap"):
        await unreachable.has_gap(SESSION, after=START_CURSOR)


def test_the_delays_are_checked(redis_client: Redis) -> None:
    with pytest.raises(ValueError, match="positive delays"):
        RedisSessionEvents(redis_client, prefix="p", retention=timedelta(0))
    with pytest.raises(ValueError, match="positive delays"):
        RedisSessionEvents(redis_client, prefix="p", ended_ttl=timedelta(days=2))


async def test_a_read_refuses_a_count_or_a_wait_out_of_bounds(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)
    with pytest.raises(ValueError, match="one event at least"):
        await events.read(SESSION, after=START_CURSOR, max_events=0, block_seconds=0)
    with pytest.raises(ValueError, match="one event at least"):
        await events.read(SESSION, after=START_CURSOR, max_events=1, block_seconds=-1)


async def test_a_long_wait_ends_empty_within_the_client_timeout(
    redis_client: Redis, redis_key_prefix: str
) -> None:
    events = RedisSessionEvents(redis_client, prefix=redis_key_prefix)
    started = time.monotonic()

    # redis-py 8 times a socket out after 5 s: a BLOCK that long would raise.
    read = await events.read(SESSION, after=START_CURSOR, max_events=1, block_seconds=6)

    assert read == []
    assert time.monotonic() - started < 4
