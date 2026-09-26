"""The session event streams on every backend: the in-memory fake, then Redis Streams.

Each backend publishes events through the ``EventPublisher`` port and reads
them back through ``SessionEventStream``, the way the worker writes and the
SSE endpoint reads. Time matters for the retention only: the fake's clock
jumps past it, Redis's passes a retention shortened to a fraction of a second.
"""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    SessionEvent,
    SessionEventType,
    StreamedEvent,
    event_id_key,
)
from livecart_core.application.ports import START_CURSOR
from livecart_core.testing.backends import EventsBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

SESSION = UUID(int=1)
OTHER_SESSION = UUID(int=2)


def _event(number: int, *, session_id: UUID = SESSION) -> SessionEvent:
    return SessionEvent(
        type=SessionEventType.STOCK_UPDATED,
        session_id=session_id,
        occurred_at=FAKE_CLOCK_START + timedelta(seconds=number),
        correlation_id=f"correlation-{number}",
        data={"product_id": str(UUID(int=100 + number)), "available": number},
    )


async def _publish(backend: EventsBackend, *numbers: int, session_id: UUID = SESSION) -> None:
    for number in numbers:
        await backend.events.publish(_event(number, session_id=session_id))


async def _all(backend: EventsBackend, session_id: UUID = SESSION) -> list[StreamedEvent]:
    return await backend.events.read(
        session_id, after=START_CURSOR, max_events=100, block_seconds=0
    )


async def test_events_come_back_in_order_under_increasing_ids(
    session_events: EventsBackend,
) -> None:
    await _publish(session_events, 1, 2, 3)

    streamed = await _all(session_events)

    assert [item.event for item in streamed] == [_event(1), _event(2), _event(3)]
    keys = [event_id_key(item.event_id) for item in streamed]
    assert keys == sorted(set(keys))
    assert await session_events.events.cursor(SESSION) == streamed[-1].event_id


async def test_a_read_returns_what_follows_its_cursor_only(session_events: EventsBackend) -> None:
    await _publish(session_events, 1, 2)
    cursor = await session_events.events.cursor(SESSION)
    await _publish(session_events, 3, 4)

    streamed = await session_events.events.read(
        SESSION, after=cursor, max_events=100, block_seconds=0
    )
    rest = await session_events.events.read(
        SESSION, after=streamed[-1].event_id, max_events=100, block_seconds=0
    )

    assert [item.event for item in streamed] == [_event(3), _event(4)]
    assert rest == []


async def test_a_read_holds_at_most_its_count(session_events: EventsBackend) -> None:
    await _publish(session_events, 1, 2, 3)

    first = await session_events.events.read(
        SESSION, after=START_CURSOR, max_events=2, block_seconds=0
    )
    second = await session_events.events.read(
        SESSION, after=first[-1].event_id, max_events=2, block_seconds=0
    )

    assert [item.event for item in first + second] == [_event(1), _event(2), _event(3)]


async def test_each_session_has_a_stream_of_its_own(session_events: EventsBackend) -> None:
    await _publish(session_events, 1)
    await _publish(session_events, 2, session_id=OTHER_SESSION)

    assert [item.event for item in await _all(session_events)] == [_event(1)]
    assert [item.event for item in await _all(session_events, OTHER_SESSION)] == [
        _event(2, session_id=OTHER_SESSION)
    ]


async def test_a_session_without_events_starts_at_the_start_cursor(
    session_events: EventsBackend,
) -> None:
    assert await session_events.events.cursor(SESSION) == START_CURSOR
    assert await _all(session_events) == []


async def test_a_blocked_read_returns_the_event_published_meanwhile(
    session_events: EventsBackend,
) -> None:
    await _publish(session_events, 1)
    cursor = await session_events.events.cursor(SESSION)

    reading = asyncio.create_task(
        session_events.events.read(SESSION, after=cursor, max_events=10, block_seconds=5)
    )
    await asyncio.sleep(0.1)
    await _publish(session_events, 2)
    streamed = await asyncio.wait_for(reading, timeout=3)

    assert [item.event for item in streamed] == [_event(2)]


async def test_a_blocked_read_gives_up_after_its_wait(session_events: EventsBackend) -> None:
    await _publish(session_events, 1)
    cursor = await session_events.events.cursor(SESSION)

    streamed = await asyncio.wait_for(
        session_events.events.read(SESSION, after=cursor, max_events=10, block_seconds=0.1),
        timeout=3,
    )

    assert streamed == []


async def test_no_gap_while_every_event_after_the_cursor_is_kept(
    session_events: EventsBackend,
) -> None:
    await _publish(session_events, 1, 2)
    [first, second] = await _all(session_events)

    for cursor in (START_CURSOR, first.event_id, second.event_id):
        assert not await session_events.events.has_gap(SESSION, after=cursor)


async def test_events_past_the_retention_are_trimmed_and_leave_a_gap(
    session_events: EventsBackend,
) -> None:
    await _publish(session_events, 1)
    [trimmed] = await _all(session_events)
    await session_events.let_retention_pass()

    await _publish(session_events, 2, 3)

    kept = await _all(session_events)
    assert [item.event for item in kept] == [_event(2), _event(3)]
    # A reader at the start missed event 1. One whose cursor is event 1 missed
    # nothing, but the stream cannot tell: on the safe side, that is a gap too.
    assert await session_events.events.has_gap(SESSION, after=START_CURSOR)
    assert await session_events.events.has_gap(SESSION, after=trimmed.event_id)
    assert not await session_events.events.has_gap(SESSION, after=kept[0].event_id)


async def test_an_expired_stream_is_a_gap_for_every_cursor(session_events: EventsBackend) -> None:
    await _publish(session_events, 1)
    [last] = await _all(session_events)

    await session_events.expire(SESSION)

    assert await session_events.events.cursor(SESSION) == START_CURSOR
    assert await session_events.events.has_gap(SESSION, after=START_CURSOR)
    assert await session_events.events.has_gap(SESSION, after=last.event_id)


async def test_a_cursor_past_the_last_event_is_a_gap(session_events: EventsBackend) -> None:
    await _publish(session_events, 1)
    [last] = await _all(session_events)
    milliseconds, sequence = event_id_key(last.event_id)

    assert await session_events.events.has_gap(SESSION, after=f"{milliseconds + 60_000}-{sequence}")


@pytest.mark.parametrize("cursor", ["", "12", "1-2-3", "a-1", "-1-0"])
async def test_a_malformed_cursor_is_refused(session_events: EventsBackend, cursor: str) -> None:
    with pytest.raises(ValueError, match="event id"):
        await session_events.events.read(SESSION, after=cursor, max_events=1, block_seconds=0)
    with pytest.raises(ValueError, match="event id"):
        await session_events.events.has_gap(SESSION, after=cursor)
