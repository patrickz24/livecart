"""The live stream of a session's events: ``GET /api/v1/live-sessions/{id}/events`` (SSE).

For the seller who owns the session, with ``Authorization: Bearer <access
token>`` (the dashboard reads it with ``fetch``, since ``EventSource`` sends no
header); another seller's session answers 404, as an id that names nothing.
FastAPI's own Server-Sent Events: ``EventSourceResponse`` on an async
generator of ``ServerSentEvent``, which sends ``: ping`` whenever 15 s pass
without a message, and ``Cache-Control: no-cache`` with ``X-Accel-Buffering:
no``, so that nginx relays every message at once. What the stream says is
``WatchLiveSession``'s (``livecart_core.application.watch_live_session``):

- ``event: snapshot``, first, without ``Last-Event-ID``: the session, its
  products and stock, its latest intents, rejections and orders, and
  ``events_cursor``, read before the snapshot; its SSE id is that cursor;
- one message per session event after the cursor, ``event:`` its type,
  ``id:`` its stream id, which a reconnection sends back as
  ``Last-Event-ID`` to resume without a new snapshot;
- ``event: stream.gap`` when events after that id are gone, a new snapshot
  right after it;
- ``event: stream.close`` at the latest 5 s before the access token
  expires, or after an hour: the client refreshes its token and reconnects
  with its last id.

Each instance serves at most ``SSE_MAX_STREAMS`` (200) streams at once, each
holding one Redis connection in ``XREAD BLOCK``: the next one answers 503
with ``Retry-After``. A stream counts once against the seller's rate limit,
when it opens.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import AwareDatetime, BaseModel, Field, JsonValue

from livecart_api.deps import CurrentSeller
from livecart_api.errors import documented_json
from livecart_api.rate_limits import TOO_MANY_REQUESTS
from livecart_api.routes.live_sessions import PREFIX, LiveSessionResponse
from livecart_api.routes.products import ProductResponse
from livecart_core.application.dto import EVENT_ID_PATTERN, SessionEventType, StreamedEvent
from livecart_core.application.ports import Clock
from livecart_core.application.session_events import (
    IntentPayload,
    OrderPayload,
    RejectionPayload,
)
from livecart_core.application.watch_live_session import (
    CloseReason,
    FeedClose,
    FeedGap,
    FeedMessage,
    FeedSnapshot,
    WatchLiveSession,
)

EVENTS_ROUTE: Final = f"{PREFIX}/{{session_id}}/events"
SNAPSHOT_EVENT: Final = "snapshot"
GAP_EVENT: Final = "stream.gap"
CLOSE_EVENT: Final = "stream.close"
STREAMS_FULL: Final = "This instance serves as many event streams as it may: try again later"
STREAMS_FULL_RETRY_AFTER_S: Final = 5
# What a client waits before it reconnects after a close, in milliseconds (SSE retry).
RECONNECT_AFTER_MS: Final = 1_000

LastEventId = Annotated[
    str | None,
    Header(
        alias="Last-Event-ID",
        pattern=f"^{EVENT_ID_PATTERN.pattern}$",
        max_length=41,
        description="The id of the last message received, to resume after it without a snapshot.",
    ),
]


class StreamSlots:
    """The streams one instance serves at once, at most ``limit``; the metrics read the counts."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("an instance serves one stream at least")
        self.limit = limit
        self.open = 0
        self.opened_total = 0
        self.refused_total = 0

    def acquire(self) -> bool:
        if self.open >= self.limit:
            self.refused_total += 1
            return False
        self.open += 1
        self.opened_total += 1
        return True

    def release(self) -> None:
        self.open -= 1


class LiveSessionSnapshot(BaseModel):
    """The first message of a stream, ``event: snapshot``: what the dashboard starts from.

    The lists hold the payloads of the matching events, newest first, at most
    ``limit`` of each; ``events_cursor`` is the id of the last event before
    the snapshot was read, which the events that follow come after.
    """

    session: LiveSessionResponse
    events_cursor: str = Field(pattern=f"^{EVENT_ID_PATTERN.pattern}$")
    products: list[ProductResponse]
    intents: list[IntentPayload]
    rejections: list[RejectionPayload]
    orders: list[OrderPayload]
    limit: int = Field(ge=1, description="The most items of each list.")
    taken_at: AwareDatetime

    @classmethod
    def of(cls, snapshot: FeedSnapshot, *, limit: int, taken_at: datetime) -> Self:
        activity = snapshot.activity
        return cls(
            session=LiveSessionResponse.of(activity.session),
            events_cursor=snapshot.cursor,
            products=[ProductResponse.of(product) for product in activity.products],
            intents=[IntentPayload.of(item.intent, item.order_id) for item in activity.intents],
            rejections=[
                RejectionPayload(comment_id=item.comment_id, reason=item.reason)
                for item in activity.rejections
            ],
            orders=[OrderPayload.of(order) for order in activity.orders],
            limit=limit,
            taken_at=taken_at,
        )


class SessionEventMessage(BaseModel):
    """A session event as the stream relays it: its type, its session, when, and its payload."""

    type: SessionEventType
    session_id: UUID
    occurred_at: AwareDatetime
    correlation_id: str | None
    data: dict[str, JsonValue]


class StreamGapData(BaseModel):
    after: str = Field(description="The Last-Event-ID the events after which are gone.")


class StreamGap(BaseModel):
    """``event: stream.gap``: events after the client's cursor are gone; a snapshot follows."""

    type: Literal["stream.gap"] = "stream.gap"
    session_id: UUID
    occurred_at: AwareDatetime
    data: StreamGapData


class StreamCloseData(BaseModel):
    reason: CloseReason


class StreamClose(BaseModel):
    """``event: stream.close``: the last message; reconnect with the last id received."""

    type: Literal["stream.close"] = "stream.close"
    session_id: UUID
    occurred_at: AwareDatetime
    data: StreamCloseData


def sse_message(
    message: FeedMessage, *, session_id: UUID, clock: Clock, snapshot_limit: int
) -> ServerSentEvent:
    """One message of the feed on the wire: its SSE event name, id and JSON data."""
    now = clock.now()
    match message:
        case FeedSnapshot():
            snapshot = LiveSessionSnapshot.of(message, limit=snapshot_limit, taken_at=now)
            return ServerSentEvent(event=SNAPSHOT_EVENT, id=message.cursor, data=snapshot)
        case StreamedEvent(event_id=event_id, event=event):
            relayed = SessionEventMessage.model_validate(event.model_dump())
            return ServerSentEvent(event=event.type.value, id=event_id, data=relayed)
        case FeedGap(after=after):
            gap = StreamGap(session_id=session_id, occurred_at=now, data=StreamGapData(after=after))
            return ServerSentEvent(event=GAP_EVENT, data=gap)
        case FeedClose(reason=reason):
            close = StreamClose(
                session_id=session_id, occurred_at=now, data=StreamCloseData(reason=reason)
            )
            return ServerSentEvent(event=CLOSE_EVENT, data=close, retry=RECONNECT_AFTER_MS)


UNAUTHORIZED: Final = "Not authenticated, or the access token was refused"
NOT_FOUND: Final = "No session of this seller has this id"


def build_session_event_router(
    watch: WatchLiveSession, slots: StreamSlots, clock: Clock, *, snapshot_limit: int
) -> APIRouter:
    router = APIRouter(
        prefix=PREFIX, tags=["live-sessions"], responses=documented_json({429: TOO_MANY_REQUESTS})
    )

    async def open_feed(
        session_id: UUID, seller: CurrentSeller, last_event_id: LastEventId = None
    ) -> AsyncIterator[AsyncIterator[FeedMessage]]:
        """The stream's slot, held until the response ends, and its checked first messages."""
        if not slots.acquire():
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=STREAMS_FULL,
                headers={"Retry-After": str(STREAMS_FULL_RETRY_AFTER_S)},
            )
        try:
            yield await watch.open(
                seller.id,
                session_id,
                resume_from=last_event_id,
                access_expires_at=seller.access_expires_at,
            )
        finally:
            slots.release()

    @router.get(
        "/{session_id}/events",
        summary="Stream the session's events: a snapshot, then every change (SSE)",
        response_class=EventSourceResponse,
        responses=documented_json(
            {
                401: UNAUTHORIZED,
                404: NOT_FOUND,
                503: "This instance serves its most streams, or the events are unavailable",
            }
        ),
    )
    async def stream_live_session_events(
        session_id: UUID,
        messages: Annotated[AsyncIterator[FeedMessage], Depends(open_feed)],
    ) -> AsyncIterator[ServerSentEvent]:
        async for message in messages:
            yield sse_message(
                message, session_id=session_id, clock=clock, snapshot_limit=snapshot_limit
            )

    return router
