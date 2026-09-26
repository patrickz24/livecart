"""``WatchLiveSession``: a seller's live session as one stream, a snapshot then its events.

What the SSE endpoint relays, message by message, independent of the wire
format:

1. **Opening** checks, before anything streams, that the session is the
   seller's (``RecordNotFound`` otherwise, the API's 404), and reads what
   the stream starts with, so that a failure there is an error answer
   rather than a broken stream:

   - without a cursor, the stream's cursor (``SessionEventStream.cursor``)
     then the snapshot (``LiveSessionActivity.snapshot``), in that order: an
     event published between the two is in the snapshot and read again
     after it, never missed, and the dashboard's reducer upserts;
   - with the cursor of a previous stream (``Last-Event-ID``), nothing more
     if no event after it is gone: the stream resumes where it stopped;
   - with a cursor past a gap (trimmed, or an expired stream), a
     ``FeedGap``, then a new snapshot as above.

2. **Then**, the events after the cursor, read in batches of 100 with a
   wait of at most 2 s (``SessionEventStream.read``), each yielded as the
   ``StreamedEvent`` it is, its id the cursor a reconnection resumes from.
   Once a minute the reader checks again for a gap, in case it fell behind
   the stream's 15 minutes of retention: a gap, then a new snapshot.

3. **The deadline**: the stream ends with a ``FeedClose`` at the earlier of
   the seller's access token's expiry, less 5 s, and an hour after it
   opened. The client then reconnects, with a fresh token and its cursor: a
   stream never outlives the token that opened it. A failure of the stream
   or of the database once streaming ends it the same way
   (``CloseReason.UNAVAILABLE``), so the client reconnects rather than wait.

The heartbeat is the transport's: the SSE endpoint's comment every 15 s.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from livecart_core.application.dto import SessionActivity, StreamedEvent, event_id_key
from livecart_core.application.ports import (
    SNAPSHOT_LIMIT,
    Clock,
    LiveSessionActivity,
    LiveSessionRepository,
    RecordNotFound,
    ServiceUnavailable,
    SessionEventStream,
)

# The longest a stream stays open, whatever the token.
MAX_STREAM_DURATION: Final = timedelta(hours=1)
# How long before its token's expiry a stream closes: time for a refresh.
TOKEN_MARGIN: Final = timedelta(seconds=5)
# The longest wait of one read, so that the deadline is seen in time; the
# Redis adapter keeps it under its client's socket timeout anyway.
READ_WAIT: Final = timedelta(seconds=2)
READ_BATCH: Final = 100
# How often a reader checks that the stream's retention did not overtake it.
GAP_CHECK_INTERVAL: Final = timedelta(minutes=1)


class CloseReason(StrEnum):
    """Why a stream ended: what its client does next."""

    # The access token expires within the margin: refresh it, then reconnect.
    TOKEN_EXPIRING = "token_expiring"  # noqa: S105 - a close reason, not a secret
    # The stream reached its longest duration: reconnect.
    MAX_DURATION = "max_duration"
    # The events or the database failed: reconnect after a pause.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class FeedSnapshot:
    """What the session holds, and the cursor of the stream it was read after."""

    activity: SessionActivity
    cursor: str


@dataclass(frozen=True, slots=True)
class FeedGap:
    """Events after ``after`` are gone: a new snapshot follows."""

    after: str


@dataclass(frozen=True, slots=True)
class FeedClose:
    reason: CloseReason


type FeedMessage = FeedSnapshot | StreamedEvent | FeedGap | FeedClose


class WatchLiveSession:
    """The stream of a seller's session; see the module docstring."""

    def __init__(
        self,
        sessions: LiveSessionRepository,
        activity: LiveSessionActivity,
        events: SessionEventStream,
        clock: Clock,
        *,
        snapshot_limit: int = SNAPSHOT_LIMIT,
        max_duration: timedelta = MAX_STREAM_DURATION,
        token_margin: timedelta = TOKEN_MARGIN,
        read_wait: timedelta = READ_WAIT,
        read_batch: int = READ_BATCH,
        gap_check_interval: timedelta = GAP_CHECK_INTERVAL,
    ) -> None:
        if min(max_duration, read_wait, gap_check_interval) <= timedelta(0):
            raise ValueError("a stream needs a duration, a read wait and a gap check interval")
        if snapshot_limit < 1 or read_batch < 1 or token_margin < timedelta(0):
            raise ValueError("a stream reads one row and one event at least")
        self._sessions = sessions
        self._activity = activity
        self._events = events
        self._clock = clock
        self._snapshot_limit = snapshot_limit
        self._max_duration = max_duration
        self._token_margin = token_margin
        self._read_wait = read_wait
        self._read_batch = read_batch
        self._gap_check_interval = gap_check_interval

    async def open(
        self,
        seller_id: UUID,
        session_id: UUID,
        *,
        resume_from: str | None,
        access_expires_at: datetime,
    ) -> AsyncIterator[FeedMessage]:
        """Check and start the stream of the seller's session; see the module docstring.

        Raises ``RecordNotFound`` for a session that is not the seller's,
        ``ValueError`` for a malformed ``resume_from`` and ``ServiceUnavailable``
        when the first reads fail, before any message.
        """
        if resume_from is not None:
            event_id_key(resume_from)
        session = await self._sessions.get_for_seller(seller_id, session_id)
        if session is None:
            raise RecordNotFound(f"no live session {session_id} for this seller")
        now = self._clock.now()
        token_deadline = access_expires_at - self._token_margin
        duration_deadline = now + self._max_duration
        if token_deadline <= duration_deadline:
            deadline, reason = token_deadline, CloseReason.TOKEN_EXPIRING
        else:
            deadline, reason = duration_deadline, CloseReason.MAX_DURATION
        if deadline <= now:
            return self._closed(reason)
        opening, cursor = await self._opening(session.id, resume_from)
        return self._messages(session.id, opening, cursor, deadline, reason)

    async def _opening(
        self, session_id: UUID, resume_from: str | None
    ) -> tuple[list[FeedMessage], str]:
        """The first messages, and the cursor the events are read after once they are sent.

        A snapshot; a gap then a snapshot; or, resuming without a gap, nothing
        before the events that follow the client's own cursor.
        """
        if resume_from is None:
            snapshot = await self._snapshot(session_id)
            return [snapshot], snapshot.cursor
        if await self._events.has_gap(session_id, after=resume_from):
            snapshot = await self._snapshot(session_id)
            return [FeedGap(resume_from), snapshot], snapshot.cursor
        return [], resume_from

    async def _snapshot(self, session_id: UUID) -> FeedSnapshot:
        # The cursor first: whatever it misses, the snapshot read after it holds.
        cursor = await self._events.cursor(session_id)
        activity = await self._activity.snapshot(session_id, limit=self._snapshot_limit)
        return FeedSnapshot(activity, cursor)

    @staticmethod
    async def _closed(reason: CloseReason) -> AsyncIterator[FeedMessage]:
        yield FeedClose(reason)

    async def _messages(
        self,
        session_id: UUID,
        opening: list[FeedMessage],
        cursor: str,
        deadline: datetime,
        reason: CloseReason,
    ) -> AsyncIterator[FeedMessage]:
        for message in opening:
            yield message
        checked_at = self._clock.now()
        while True:
            now = self._clock.now()
            if now >= deadline:
                yield FeedClose(reason)
                return
            try:
                if now - checked_at >= self._gap_check_interval:
                    checked_at = now
                    if await self._events.has_gap(session_id, after=cursor):
                        snapshot = await self._snapshot(session_id)
                        yield FeedGap(cursor)
                        yield snapshot
                        cursor = snapshot.cursor
                        continue
                wait = min(self._read_wait, deadline - now)
                events = await self._events.read(
                    session_id,
                    after=cursor,
                    max_events=self._read_batch,
                    block_seconds=wait.total_seconds(),
                )
            except (ServiceUnavailable, RecordNotFound):
                yield FeedClose(CloseReason.UNAVAILABLE)
                return
            for streamed in events:
                yield streamed
                cursor = streamed.event_id
