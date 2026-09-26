"""What only the session events fake promises: its record of what was published, its failures."""

from uuid import UUID

import pytest

from livecart_core.application.dto import SessionEvent, SessionEventType
from livecart_core.application.ports import START_CURSOR, ServiceUnavailable
from livecart_core.testing.fakes import FAKE_CLOCK_START, FakeClock, InMemorySessionEvents

SESSION = UUID(int=1)


def _event(event_type: SessionEventType) -> SessionEvent:
    return SessionEvent(type=event_type, session_id=SESSION, occurred_at=FAKE_CLOCK_START)


async def test_published_events_are_recorded_in_order_by_type() -> None:
    events = InMemorySessionEvents(FakeClock())

    await events.publish(_event(SessionEventType.ORDER_UPDATED))
    await events.publish(_event(SessionEventType.STOCK_UPDATED))

    assert [event.type for event in events.published] == [
        SessionEventType.ORDER_UPDATED,
        SessionEventType.STOCK_UPDATED,
    ]
    assert events.of_type(SessionEventType.STOCK_UPDATED) == [
        _event(SessionEventType.STOCK_UPDATED)
    ]


async def test_scripted_failures_append_nothing() -> None:
    events = InMemorySessionEvents(FakeClock())
    events.fail_next(4)

    with pytest.raises(ServiceUnavailable):
        await events.publish(_event(SessionEventType.ORDER_UPDATED))
    with pytest.raises(ServiceUnavailable):
        await events.cursor(SESSION)
    with pytest.raises(ServiceUnavailable):
        await events.read(SESSION, after=START_CURSOR, max_events=1, block_seconds=0)
    with pytest.raises(ServiceUnavailable):
        await events.has_gap(SESSION, after=START_CURSOR)

    assert events.published == []
    assert await events.cursor(SESSION) == START_CURSOR


async def test_a_read_refuses_a_count_or_a_wait_out_of_bounds() -> None:
    events = InMemorySessionEvents(FakeClock())

    with pytest.raises(ValueError, match="one event at least"):
        await events.read(SESSION, after=START_CURSOR, max_events=0, block_seconds=0)
    with pytest.raises(ValueError, match="one event at least"):
        await events.read(SESSION, after=START_CURSOR, max_events=1, block_seconds=-1)
