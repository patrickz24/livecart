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
holding one Redis connection in ``XREAD BLOCK``, from a pool of as many
connections that only the streams use (``Resources.event_streams``), so that
they never take the shared pool's from the webhook, the login or ``/ready``:
the next one answers 503 with ``Retry-After``. One seller holds at most
``SSE_MAX_STREAMS_PER_SELLER`` (5) of them on an instance, its next one
answering 429 with ``Retry-After``, so that no seller can take every slot
from the others. A stream counts once against the seller's rate limit, when
it opens.
"""

from collections import Counter
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.openapi.constants import REF_PREFIX
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel, ValidationError
from pydantic.json_schema import models_json_schema

from livecart_api.deps import CurrentSeller
from livecart_api.errors import documented_json
from livecart_api.routes.live_sessions import PREFIX, LiveSessionResponse
from livecart_api.routes.products import ProductResponse
from livecart_core.application.dto import EVENT_ID_PATTERN, SessionEventType, StreamedEvent
from livecart_core.application.ports import Clock
from livecart_core.application.session_events import (
    IntentPayload,
    OrderPayload,
    RejectionPayload,
    SessionPayload,
    ShipmentPayload,
    StockPayload,
)
from livecart_core.application.watch_live_session import (
    CloseReason,
    FeedClose,
    FeedGap,
    FeedMessage,
    FeedSnapshot,
    WatchLiveSession,
)

logger = structlog.get_logger(__name__)

EVENTS_ROUTE: Final = f"{PREFIX}/{{session_id}}/events"
SNAPSHOT_EVENT: Final = "snapshot"
GAP_EVENT: Final = "stream.gap"
CLOSE_EVENT: Final = "stream.close"
STREAMS_FULL: Final = "This instance serves as many event streams as it may: try again later"
SELLER_STREAMS_FULL: Final = (
    "This seller holds as many event streams as it may on this instance: close one, "
    "or try again later"
)
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


class StreamRefusal(StrEnum):
    """Why a stream got no slot: what its answer says, and the label of its metric."""

    # The seller holds SSE_MAX_STREAMS_PER_SELLER streams on this instance: 429,
    # its own excess, checked first so that it is told so even on a full instance.
    SELLER_LIMIT = "seller_limit"
    # The instance serves SSE_MAX_STREAMS streams: 503, its capacity.
    INSTANCE_FULL = "instance_full"


class StreamSlots:
    """The streams one instance serves at once: ``limit`` in all, ``per_seller`` of one seller.

    The metrics read the counts. A seller's streams are counted by its id
    while they are open, so that one seller, whose dashboard needs one or
    two, can never take every slot of the instance from the others.
    """

    def __init__(self, limit: int, *, per_seller: int) -> None:
        if limit < 1 or per_seller < 1:
            raise ValueError("an instance serves one stream at least, and one of each seller")
        self.limit = limit
        self.per_seller = per_seller
        self.open = 0
        self.opened_total = 0
        self.refused_total: Counter[StreamRefusal] = Counter(dict.fromkeys(StreamRefusal, 0))
        self._by_seller: dict[UUID, int] = {}

    def held_by(self, seller_id: UUID) -> int:
        """The streams of the seller open on this instance."""
        return self._by_seller.get(seller_id, 0)

    def acquire(self, seller_id: UUID) -> StreamRefusal | None:
        """Take a slot for one of the seller's streams: ``None`` once taken, or why it is not."""
        held = self.held_by(seller_id)
        if held >= self.per_seller:
            refusal = StreamRefusal.SELLER_LIMIT
        elif self.open >= self.limit:
            refusal = StreamRefusal.INSTANCE_FULL
        else:
            self._by_seller[seller_id] = held + 1
            self.open += 1
            self.opened_total += 1
            return None
        self.refused_total[refusal] += 1
        return refusal

    def release(self, seller_id: UUID) -> None:
        """Give back a slot the seller took."""
        held = self.held_by(seller_id)
        if held < 1:
            raise RuntimeError("this seller holds no stream slot")
        if held == 1:
            del self._by_seller[seller_id]
        else:
            self._by_seller[seller_id] = held - 1
        self.open -= 1


# The answer to each refusal: the seller's own excess is a 429, the instance's capacity a 503.
REFUSALS: Final = {
    StreamRefusal.SELLER_LIMIT: (status.HTTP_429_TOO_MANY_REQUESTS, SELLER_STREAMS_FULL),
    StreamRefusal.INSTANCE_FULL: (status.HTTP_503_SERVICE_UNAVAILABLE, STREAMS_FULL),
}


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


# --- The LiveSessionEvent union: every message of a stream but its snapshot ---------------
# Each member shares the envelope of a SessionEvent (type, session, time, the
# comment's correlation id, if any) and holds its own payload in ``data``.


class _Envelope(BaseModel):
    # Every field is in every message, defaults included: the schema says so.
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_serialization_defaults_required=True
    )

    session_id: UUID
    occurred_at: AwareDatetime
    correlation_id: str | None = None


class IntentRecorded(_Envelope):
    """A comment settled on a product: its intent, reserved or not, and its order if one opened."""

    type: Literal["intent.recorded"] = "intent.recorded"
    data: IntentPayload


class CommentRejected(_Envelope):
    """A comment of the session that carries no intent, and why."""

    type: Literal["comment.rejected"] = "comment.rejected"
    data: RejectionPayload


class OrderUpdated(_Envelope):
    """An order opened, paid or expired."""

    type: Literal["order.updated"] = "order.updated"
    data: OrderPayload


class StockUpdated(_Envelope):
    """A product's counters after a reservation, a sale or a release."""

    type: Literal["stock.updated"] = "stock.updated"
    data: StockPayload


class ShipmentUpdated(_Envelope):
    """A paid order's shipment, as its carrier reports it (milestone 6)."""

    type: Literal["shipment.updated"] = "shipment.updated"
    data: ShipmentPayload


class SessionUpdated(_Envelope):
    """The session started or ended."""

    type: Literal["session.updated"] = "session.updated"
    data: SessionPayload


class StreamGapData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    after: str = Field(description="The Last-Event-ID the events after which are gone.")


class StreamGap(_Envelope):
    """Events after the client's cursor are gone: drop what it holds, a snapshot follows."""

    type: Literal["stream.gap"] = "stream.gap"
    data: StreamGapData


class StreamCloseData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: CloseReason


class StreamClose(_Envelope):
    """The last message: reconnect with the last id received (a fresh token first, if expiring)."""

    type: Literal["stream.close"] = "stream.close"
    data: StreamCloseData


# A plain alias, not a "type" statement: pydantic then inlines the union in
# LiveSessionEvent, where a "type" alias would become a component of its own.
LiveSessionEventMember = Annotated[
    IntentRecorded
    | CommentRejected
    | OrderUpdated
    | StockUpdated
    | ShipmentUpdated
    | SessionUpdated
    | StreamGap
    | StreamClose,
    Field(discriminator="type"),
]


class LiveSessionEvent(RootModel[LiveSessionEventMember]):
    """Every message of a stream after its snapshot, discriminated by ``type``.

    ``type`` is also the message's SSE event name. The first six are the
    session's events, relayed with their stream id as the SSE id; the last
    two are the stream's own, without an id.
    """


# The types of the session's own events: those a stream relays from Redis.
RELAYED_TYPES: Final = frozenset(event_type.value for event_type in SessionEventType)
# The stream's messages, added to the OpenAPI components (LiveCartApi.openapi).
STREAM_MODELS: Final[tuple[type[BaseModel], ...]] = (LiveSessionSnapshot, LiveSessionEvent)


def add_components(schema: dict[str, Any], models: Sequence[type[BaseModel]]) -> None:
    """Put the JSON schemas of ``models``, and of every model they hold, in ``schema``'s components.

    A component FastAPI generated already, such as ``ProductResponse``, must
    come out the same: two definitions under one name would give the
    generated client one of them at random.
    """
    _, definitions = models_json_schema(
        [(model, "serialization") for model in models],
        ref_template=f"{REF_PREFIX}{{model}}",
    )
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in definitions.get("$defs", {}).items():
        if components.setdefault(name, definition) != definition:
            raise RuntimeError(f"two definitions of the OpenAPI component {name}")


def stream_item_schema() -> dict[str, object]:
    """What each SSE item's data holds: a snapshot first, then ``LiveSessionEvent`` messages."""
    return {
        "required": ["event", "data"],
        "properties": {
            "data": {
                "type": "string",
                "contentMediaType": "application/json",
                "contentSchema": {
                    "oneOf": [{"$ref": f"{REF_PREFIX}{model.__name__}"} for model in STREAM_MODELS]
                },
            },
            "event": {
                "type": "string",
                "enum": [SNAPSHOT_EVENT, *sorted(RELAYED_TYPES), GAP_EVENT, CLOSE_EVENT],
            },
        },
    }


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
            relayed = LiveSessionEvent.model_validate(event.model_dump(mode="json"))
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
    router = APIRouter(prefix=PREFIX, tags=["live-sessions"])

    async def open_feed(
        session_id: UUID, seller: CurrentSeller, last_event_id: LastEventId = None
    ) -> AsyncIterator[AsyncIterator[FeedMessage]]:
        """The stream's slot, held until the response ends, and its checked first messages."""
        refusal = slots.acquire(seller.id)
        if refusal is not None:
            logger.info("session_stream_refused", reason=refusal.value)
            code, detail = REFUSALS[refusal]
            raise HTTPException(
                code, detail=detail, headers={"Retry-After": str(STREAMS_FULL_RETRY_AFTER_S)}
            )
        try:
            yield await watch.open(
                seller.id,
                session_id,
                resume_from=last_event_id,
                access_expires_at=seller.access_expires_at,
            )
        finally:
            slots.release(seller.id)

    @router.get(
        "/{session_id}/events",
        summary="Stream the session's events: a snapshot, then every change (SSE)",
        response_class=EventSourceResponse,
        responses={
            200: {
                "description": (
                    "The session's stream: event snapshot (LiveSessionSnapshot), then "
                    "LiveSessionEvent messages, each named by its type"
                ),
                "content": {"text/event-stream": {"itemSchema": stream_item_schema()}},
            },
            **documented_json(
                {
                    401: UNAUTHORIZED,
                    404: NOT_FOUND,
                    429: (
                        "Too many requests, or the seller holds its most streams on this "
                        "instance: try again after Retry-After seconds"
                    ),
                    503: "This instance serves its most streams, or the events are unavailable",
                }
            ),
        },
    )
    async def stream_live_session_events(
        session_id: UUID,
        messages: Annotated[AsyncIterator[FeedMessage], Depends(open_feed)],
    ) -> AsyncIterator[ServerSentEvent]:
        async for message in messages:
            if isinstance(message, FeedClose):
                # The port errors name their operation and exception type only.
                log = logger.warning if message.error is not None else logger.info
                log(
                    "session_stream_closed",
                    session_id=str(session_id),
                    reason=message.reason.value,
                    error=message.error,
                )
            try:
                relayed = sse_message(
                    message, session_id=session_id, clock=clock, snapshot_limit=snapshot_limit
                )
            except ValidationError:
                # An event that fits no member of the union (a publisher of another
                # version): skipped and logged, rather than end every stream.
                logger.error(
                    "session_event_unrelayable",
                    session_id=str(session_id),
                    event_id=message.event_id if isinstance(message, StreamedEvent) else None,
                )
                continue
            yield relayed

    return router
