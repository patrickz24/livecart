"""The session events on Redis Streams: one stream per live session, written after each commit.

``RedisSessionEvents`` is both ports of the stream: the ``EventPublisher``
the worker (and the API, for a session's start and end) appends through,
and the ``SessionEventStream`` the SSE endpoint reads. The stream of a
session is ``{prefix}:events:session:{session_id}``; each entry holds one
``SessionEvent`` as JSON in its ``event`` field, and its entry id is the
event's id, the SSE id a client resumes from.

Publishing is one Lua script, so that its three steps are one step for
every other client:

- ``XADD ... MINID ~ <now - 15 min>``: the stream keeps about the last 15
  minutes of events, time enough for a dashboard to reconnect; an older one
  resumes from a new snapshot (``has_gap``). ``now`` is Redis's own clock
  (``TIME``), the clock that dates the entry ids, and ``~`` lets Redis trim
  whole nodes only;
- ``EXPIRE 86400``: a stream goes a day after its last event, so a session
  nobody ends does not keep its stream for ever;
- once the session has ended (a ``session.updated`` event with the status
  ``ended``), ``EXPIRE 3600`` instead, for that event and every later one
  (an order expiring after the end): a stream whose time to live is an hour
  or less stays on that hour. A live stream idle for 23 hours falls under
  that rule too, harmlessly: once it expires, its readers take a snapshot.

A publication happens after its commit, best effort: a Redis failure is
logged here with the event's type and session, raised as
``ServiceUnavailable`` for the caller to count, and never retried: the commit
stands, and the dashboard catches up from its next snapshot.

Reading: ``cursor`` is ``XREVRANGE + - COUNT 1``, ``read`` is ``XREAD COUNT
n BLOCK ms``, which waits for the stream to exist if it does not yet. Its
wait stays under ``MAX_READ_BLOCK`` (2 s), whatever the caller asks: the
client's socket times out after 5 s (redis-py 8's default), and a longer
``BLOCK`` would end in a ``TimeoutError`` instead of an empty read, as the
comment queue's ``XREADGROUP BLOCK 2000`` also avoids. Then
``has_gap`` reads ``XINFO STREAM``. Trimming does not record the ids it
removed (``max-deleted-entry-id`` only follows ``XDEL``), so the check errs
on the safe side: a cursor older than the stream's first event is a gap as
soon as the stream has lost an entry (``entries-added`` above ``length``),
or when it is not the start cursor (a stream started again since the old
one expired). So is a stream that no longer exists, and a cursor past the
last id. A false gap costs its reader one snapshot; a missed one would lose
events silently.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from livecart_core.application.dto import (
    SessionEvent,
    SessionEventType,
    StreamedEvent,
    event_id_key,
)
from livecart_core.application.ports import START_CURSOR, ServiceUnavailable
from livecart_core.domain.state_machines import LiveSessionStatus

logger = structlog.get_logger(__name__)

EVENT_FIELD: Final = "event"
# The longest XREAD BLOCK: under the client's 5 s socket timeout.
MAX_READ_BLOCK: Final = timedelta(seconds=2)
# How long a stream keeps an event, then how long it outlives its last one.
EVENT_RETENTION: Final = timedelta(minutes=15)
LIVE_STREAM_TTL: Final = timedelta(hours=24)
ENDED_STREAM_TTL: Final = timedelta(hours=1)

# KEYS: the stream. ARGV: the event's JSON, the retention in ms, the TTLs of a
# live and of an ended session's stream in s, "1" when this event ends the
# session, "~" or "=" for the trim.
PUBLISH_SCRIPT: Final = """
local time = redis.call("TIME")
local now_ms = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local minid = tostring(now_ms - tonumber(ARGV[2]))
local id = redis.call("XADD", KEYS[1], "MINID", ARGV[6], minid, "*", "event", ARGV[1])
local ttl = redis.call("TTL", KEYS[1])
if ARGV[5] == "1" or (ttl >= 0 and ttl <= tonumber(ARGV[4])) then
  redis.call("EXPIRE", KEYS[1], ARGV[4])
else
  redis.call("EXPIRE", KEYS[1], ARGV[3])
end
return id
"""

type Entry = tuple[bytes, Mapping[bytes, bytes]]


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def stream_gap(
    cursor: tuple[int, int],
    *,
    last: tuple[int, int],
    first: tuple[int, int],
    length: int,
    added: int,
) -> bool:
    """Whether a reader at ``cursor`` may have missed events of an existing stream.

    The stream removes its oldest entries only, so every removed id is below
    ``first``: a cursor at or past it missed nothing.
    """
    if cursor > last:
        return True
    if length == 0:
        return cursor < last
    if cursor >= first:
        return False
    return added > length or cursor != (0, 0)


def ends_session(event: SessionEvent) -> bool:
    """Whether ``event`` says its session has ended: its stream then shortens to an hour."""
    return (
        event.type is SessionEventType.SESSION_UPDATED
        and event.data.get("status") == LiveSessionStatus.ENDED.value
    )


@asynccontextmanager
async def _redis_errors(operation: str) -> AsyncIterator[None]:
    try:
        yield
    except RedisError as error:
        raise ServiceUnavailable(
            f"session events unavailable ({operation}): {type(error).__name__}"
        ) from None


class RedisSessionEvents:
    """The streams of the live sessions' events; see the module docstring."""

    def __init__(
        self,
        client: Redis,
        *,
        prefix: str,
        retention: timedelta = EVENT_RETENTION,
        live_ttl: timedelta = LIVE_STREAM_TTL,
        ended_ttl: timedelta = ENDED_STREAM_TTL,
        approximate_trim: bool = True,
    ) -> None:
        if min(retention, live_ttl, ended_ttl) <= timedelta(0) or ended_ttl >= live_ttl:
            raise ValueError("positive delays, an ended stream's shorter than a live one's")
        self._client = client
        self._prefix = prefix
        self._retention_ms = int(retention.total_seconds() * 1000)
        self._live_ttl_s = int(live_ttl.total_seconds())
        self._ended_ttl_s = int(ended_ttl.total_seconds())
        # Exact trimming only serves the tests, which check the retention's
        # boundary; "~" lets Redis drop whole nodes only, which costs far less.
        self._trim = "~" if approximate_trim else "="
        self._publish_script = client.register_script(PUBLISH_SCRIPT)

    def stream_key(self, session_id: UUID) -> str:
        return f"{self._prefix}:events:session:{session_id}"

    async def publish(self, event: SessionEvent) -> None:
        args: list[str | int] = [
            event.model_dump_json(),
            self._retention_ms,
            self._live_ttl_s,
            self._ended_ttl_s,
            "1" if ends_session(event) else "0",
            self._trim,
        ]
        try:
            await self._publish_script(keys=[self.stream_key(event.session_id)], args=args)
        except RedisError as error:
            logger.warning(
                "session_event_unpublished",
                event_type=event.type.value,
                session_id=str(event.session_id),
                error=type(error).__name__,
            )
            raise ServiceUnavailable(
                f"session events unavailable (publish): {type(error).__name__}"
            ) from None

    async def cursor(self, session_id: UUID) -> str:
        async with _redis_errors("cursor"):
            entries: Any = await self._client.xrevrange(self.stream_key(session_id), count=1)
        return _text(entries[0][0]) if entries else START_CURSOR

    async def read(
        self, session_id: UUID, *, after: str, max_events: int, block_seconds: float
    ) -> list[StreamedEvent]:
        event_id_key(after)
        if max_events < 1 or block_seconds < 0:
            raise ValueError("a read takes one event at least, and waits zero seconds or more")
        # BLOCK 0 would wait for ever: no wait is no BLOCK at all, any wait 1 ms at
        # least, and none longer than the client's socket lets a reply take.
        wait_s = min(block_seconds, MAX_READ_BLOCK.total_seconds())
        block = max(1, round(wait_s * 1000)) if wait_s > 0 else None
        key = self.stream_key(session_id)
        async with _redis_errors("read"):
            response: Any = await self._client.xread({key: after}, count=max_events, block=block)
        # RESP2, the client's protocol: [[stream key, [entry, ...]]], or nothing.
        entries: list[Entry] = list(response[0][1]) if response else []
        return [streamed for entry in entries if (streamed := self._streamed(entry)) is not None]

    async def has_gap(self, session_id: UUID, *, after: str) -> bool:
        cursor = event_id_key(after)
        async with _redis_errors("has gap"):
            try:
                info: Any = await self._client.xinfo_stream(self.stream_key(session_id))
            except ResponseError:
                return True  # no such key: the stream expired, or never existed
        return stream_gap(
            cursor,
            last=event_id_key(_text(info["last-generated-id"])),
            first=event_id_key(_text(info["recorded-first-entry-id"])),
            length=int(info["length"]),
            added=int(info["entries-added"]),
        )

    def _streamed(self, entry: Entry) -> StreamedEvent | None:
        entry_id, fields = entry
        try:
            event = SessionEvent.model_validate_json(fields.get(EVENT_FIELD.encode(), b""))
        except ValidationError:
            # Written by an older or foreign publisher: skipped, never relayed half-read.
            logger.error("session_event_malformed", event_id=_text(entry_id))
            return None
        return StreamedEvent(_text(entry_id), event)


class NullEventPublisher:
    """Publishes nothing, and never fails: for a process that has no stream to write to."""

    async def publish(self, event: SessionEvent) -> None:
        return None
