"""``WatchLiveSession`` over the fakes: snapshot and cursor, events, gaps and the deadline.

Reads wait 20 ms of real time at most, and the fake clock decides every
deadline: a test moves it past one, and the next read sees it.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    LiveSessionRecord,
    SessionEvent,
    SessionEventType,
    StreamedEvent,
)
from livecart_core.application.ports import START_CURSOR, RecordNotFound, ServiceUnavailable
from livecart_core.application.watch_live_session import (
    MAX_STREAM_DURATION,
    TOKEN_MARGIN,
    CloseReason,
    FeedClose,
    FeedGap,
    FeedMessage,
    FeedSnapshot,
    WatchLiveSession,
)
from livecart_core.testing.fakes import FakeClock, InMemoryDatabase, InMemorySessionEvents

READ_WAIT = timedelta(milliseconds=20)


@dataclass
class World:
    database: InMemoryDatabase
    clock: FakeClock
    events: InMemorySessionEvents
    session: LiveSessionRecord
    watch: WatchLiveSession

    @property
    def token_expiry(self) -> datetime:
        """An access token of 15 minutes, as the API issues them."""
        return self.clock.now() + timedelta(minutes=15)

    async def publish(self, available: int) -> SessionEvent:
        event = SessionEvent(
            type=SessionEventType.STOCK_UPDATED,
            session_id=self.session.id,
            occurred_at=self.clock.now(),
            data={"available": available},
        )
        await self.events.publish(event)
        return event

    async def open(
        self, *, resume_from: str | None = None, expires_at: datetime | None = None
    ) -> AsyncIterator[FeedMessage]:
        return await self.watch.open(
            self.session.seller_id,
            self.session.id,
            resume_from=resume_from,
            access_expires_at=expires_at if expires_at is not None else self.token_expiry,
        )


def _world(*, gap_check_interval: timedelta = timedelta(minutes=1)) -> World:
    database = InMemoryDatabase()
    clock = FakeClock()
    events = InMemorySessionEvents(clock)
    session = database.add_live_session()
    database.add_product(session.id, "B07", stock_total=3)
    watch = WatchLiveSession(
        database.live_sessions,
        database.activity,
        events,
        clock,
        read_wait=READ_WAIT,
        gap_check_interval=gap_check_interval,
    )
    return World(database, clock, events, session, watch)


async def _snapshot(messages: AsyncIterator[FeedMessage]) -> FeedSnapshot:
    message = await anext(messages)
    assert isinstance(message, FeedSnapshot)
    return message


async def _event(messages: AsyncIterator[FeedMessage]) -> StreamedEvent:
    message = await anext(messages)
    assert isinstance(message, StreamedEvent)
    return message


async def test_a_stream_starts_with_the_snapshot_then_relays_the_events_after_its_cursor() -> None:
    world = _world()
    before = await world.publish(3)
    cursor = await world.events.cursor(world.session.id)

    messages = await world.open()
    snapshot = await _snapshot(messages)
    after = await world.publish(2)

    assert snapshot.cursor == cursor
    assert snapshot.activity.session == world.session
    assert [product.code.value for product in snapshot.activity.products] == ["B07"]
    # The event before the cursor is the snapshot's; the one after it streams.
    streamed = await _event(messages)
    assert streamed.event == after
    assert streamed.event != before


async def test_the_cursor_of_an_empty_stream_is_the_start() -> None:
    world = _world()

    snapshot = await _snapshot(await world.open())

    assert snapshot.cursor == START_CURSOR


async def test_a_stream_resumes_after_its_cursor_without_a_new_snapshot() -> None:
    world = _world()
    await world.publish(3)
    cursor = await world.events.cursor(world.session.id)
    missed = await world.publish(2)

    messages = await world.open(resume_from=cursor)

    assert (await _event(messages)).event == missed


async def test_a_cursor_past_a_gap_gets_the_gap_then_a_new_snapshot() -> None:
    world = _world()
    await world.publish(3)
    cursor = await world.events.cursor(world.session.id)
    world.events.expire(world.session.id)

    messages = await world.open(resume_from=cursor)

    assert await anext(messages) == FeedGap(cursor)
    assert (await _snapshot(messages)).cursor == START_CURSOR


async def test_a_reader_overtaken_by_the_retention_gets_a_gap_and_a_new_snapshot() -> None:
    world = _world()
    messages = await world.open(expires_at=world.clock.now() + timedelta(hours=1))
    await _snapshot(messages)
    first = await world.publish(3)
    assert (await _event(messages)).event == first
    # Twenty minutes later, the next event trims the one the reader stopped at.
    world.clock.advance(timedelta(minutes=20))
    await world.publish(2)

    message = await anext(messages)

    assert isinstance(message, FeedGap)
    snapshot = await _snapshot(messages)
    assert snapshot.cursor == await world.events.cursor(world.session.id)
    # The stream goes on from the new snapshot's cursor.
    later = await world.publish(1)
    assert (await _event(messages)).event == later


async def test_a_gap_check_that_finds_none_goes_on_reading() -> None:
    world = _world(gap_check_interval=timedelta(seconds=1))
    messages = await world.open()
    await _snapshot(messages)
    await world.publish(2)
    await _event(messages)
    world.clock.advance(timedelta(seconds=2))

    event = await world.publish(1)

    assert (await _event(messages)).event == event


async def test_the_stream_closes_before_the_token_expires() -> None:
    world = _world()
    messages = await world.open()
    await _snapshot(messages)

    world.clock.advance(timedelta(minutes=15) - TOKEN_MARGIN)

    assert await anext(messages) == FeedClose(CloseReason.TOKEN_EXPIRING)
    with pytest.raises(StopAsyncIteration):
        await anext(messages)


async def test_a_long_token_closes_the_stream_after_an_hour() -> None:
    world = _world()
    messages = await world.open(expires_at=world.clock.now() + timedelta(hours=3))
    await _snapshot(messages)

    world.clock.advance(MAX_STREAM_DURATION - timedelta(seconds=1))
    await world.publish(1)
    await _event(messages)
    world.clock.advance(timedelta(seconds=1))

    assert await anext(messages) == FeedClose(CloseReason.MAX_DURATION)


async def test_a_token_about_to_expire_closes_the_stream_at_once() -> None:
    world = _world()

    messages = await world.open(expires_at=world.clock.now() + TOKEN_MARGIN)

    assert [message async for message in messages] == [FeedClose(CloseReason.TOKEN_EXPIRING)]


async def test_another_sellers_session_is_not_found() -> None:
    world = _world()

    with pytest.raises(RecordNotFound):
        await world.watch.open(
            UUID(int=999),
            world.session.id,
            resume_from=None,
            access_expires_at=world.token_expiry,
        )


@pytest.mark.parametrize("cursor", ["", "latest", "1-2-3"])
async def test_a_malformed_cursor_is_refused_before_anything(cursor: str) -> None:
    world = _world()

    with pytest.raises(ValueError, match="event id"):
        await world.open(resume_from=cursor)


async def test_a_failure_before_the_first_message_is_raised() -> None:
    world = _world()
    world.events.fail_next()

    with pytest.raises(ServiceUnavailable):
        await world.open()


async def test_a_failure_while_streaming_closes_the_stream() -> None:
    world = _world()
    messages = await world.open()
    await _snapshot(messages)

    world.events.fail_next()

    assert await anext(messages) == FeedClose(CloseReason.UNAVAILABLE)
    with pytest.raises(StopAsyncIteration):
        await anext(messages)


async def test_a_failed_gap_check_closes_the_stream_too() -> None:
    world = _world(gap_check_interval=timedelta(seconds=1))
    messages = await world.open()
    await _snapshot(messages)
    world.clock.advance(timedelta(seconds=2))

    world.events.fail_next()

    assert await anext(messages) == FeedClose(CloseReason.UNAVAILABLE)


def test_the_bounds_of_a_stream_are_checked() -> None:
    database = InMemoryDatabase()
    clock = FakeClock()
    events = InMemorySessionEvents(clock)
    args = (database.live_sessions, database.activity, events, clock)
    with pytest.raises(ValueError, match="duration"):
        WatchLiveSession(*args, read_wait=timedelta(0))
    with pytest.raises(ValueError, match="one row"):
        WatchLiveSession(*args, read_batch=0)
    with pytest.raises(ValueError, match="one row"):
        WatchLiveSession(*args, token_margin=timedelta(seconds=-1))
