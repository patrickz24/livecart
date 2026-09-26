"""A seller's live sessions: ``/api/v1/live-sessions``.

Every route declares ``CurrentSeller`` and hands the seller's id to the use
case, whose repository calls filter by it: another seller's session answers
404, exactly like an id that names nothing (never 403, which would confirm
it exists).

- ``POST /`` creates a draft; ``GET /`` lists the seller's sessions, newest
  first, a bounded page at a time (``limit`` up to 100, then the
  ``next_cursor`` of the answer as ``cursor``);
- ``GET /{id}`` and ``PATCH /{id}``: the title changes until the session
  ends, the platform and live id, which comments find the session by, only
  in a draft; an ended session changes no more (409);
- ``POST /{id}/start`` and ``/end`` move it along ``draft -> live ->
  ended``; calling one again once there answers 200 with the session as it
  is, and any other move 409.

Request bodies refuse unknown fields, and responses are models of their own,
never the records of the repositories.
"""

from datetime import datetime
from typing import Annotated, Final, Self
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints

from livecart_api.deps import CurrentSeller
from livecart_api.errors import documented
from livecart_api.rate_limits import TOO_MANY_REQUESTS
from livecart_core.application.dto import (
    MAX_IDENTIFIER_LENGTH,
    MAX_PAGE_SIZE,
    MAX_TITLE_LENGTH,
    LiveSessionChanges,
    LiveSessionRecord,
    NewLiveSession,
    Platform,
)
from livecart_core.application.live_sessions import SellerLiveSessions
from livecart_core.domain.state_machines import LiveSessionStatus

PREFIX: Final = "/api/v1/live-sessions"
DEFAULT_PAGE_SIZE: Final = 20

Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE_LENGTH)
]
LiveId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
]
PageSize = Annotated[
    int, Query(ge=1, le=MAX_PAGE_SIZE, description=f"Items per page, 1 to {MAX_PAGE_SIZE}.")
]
Cursor = Annotated[
    UUID | None, Query(description="The next_cursor of the previous page; none for the first.")
]


class LiveSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    platform: Platform
    external_live_id: LiveId


class LiveSessionUpdate(BaseModel):
    """The fields to change; a field left out, or null, keeps its value."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    platform: Platform | None = None
    external_live_id: LiveId | None = None


class LiveSessionResponse(BaseModel):
    id: UUID
    title: str
    platform: Platform
    external_live_id: str
    status: LiveSessionStatus
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None

    @classmethod
    def of(cls, session: LiveSessionRecord) -> Self:
        return cls(
            id=session.id,
            title=session.title,
            platform=session.platform,
            external_live_id=session.external_live_id,
            status=session.status,
            created_at=session.created_at,
            started_at=session.started_at,
            ended_at=session.ended_at,
        )


class LiveSessionPage(BaseModel):
    items: list[LiveSessionResponse]
    next_cursor: UUID | None


UNAUTHORIZED: Final = "Not authenticated, or the access token was refused"
NOT_FOUND: Final = "No session of this seller has this id"
CONFLICT: Final = "The session's status does not allow it, or the live id is taken"


def build_live_session_router(sessions: SellerLiveSessions) -> APIRouter:
    # CurrentSeller counts every request against the seller's rate limit.
    router = APIRouter(
        prefix=PREFIX, tags=["live-sessions"], responses=documented({429: TOO_MANY_REQUESTS})
    )

    @router.post(
        "",
        summary="Create a draft live session",
        status_code=status.HTTP_201_CREATED,
        responses=documented(
            {401: UNAUTHORIZED, 409: "Another session has this platform and live id"}
        ),
    )
    async def create_live_session(
        seller: CurrentSeller, body: LiveSessionCreate, response: Response
    ) -> LiveSessionResponse:
        session = await sessions.create(
            seller.id, NewLiveSession(body.title, body.platform, body.external_live_id)
        )
        response.headers["Location"] = f"{PREFIX}/{session.id}"
        return LiveSessionResponse.of(session)

    @router.get(
        "",
        summary="List the seller's live sessions, newest first",
        responses=documented({401: UNAUTHORIZED}),
    )
    async def list_live_sessions(
        seller: CurrentSeller, limit: PageSize = DEFAULT_PAGE_SIZE, cursor: Cursor = None
    ) -> LiveSessionPage:
        page = await sessions.list(seller.id, limit=limit, cursor=cursor)
        return LiveSessionPage(
            items=[LiveSessionResponse.of(session) for session in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/{session_id}",
        summary="Read one of the seller's live sessions",
        responses=documented({401: UNAUTHORIZED, 404: NOT_FOUND}),
    )
    async def get_live_session(seller: CurrentSeller, session_id: UUID) -> LiveSessionResponse:
        return LiveSessionResponse.of(await sessions.get(seller.id, session_id))

    @router.patch(
        "/{session_id}",
        summary="Change a live session's title, and in a draft its platform and live id",
        responses=documented({401: UNAUTHORIZED, 404: NOT_FOUND, 409: CONFLICT}),
    )
    async def update_live_session(
        seller: CurrentSeller, session_id: UUID, body: LiveSessionUpdate
    ) -> LiveSessionResponse:
        changes = LiveSessionChanges(
            title=body.title, platform=body.platform, external_live_id=body.external_live_id
        )
        return LiveSessionResponse.of(await sessions.update(seller.id, session_id, changes))

    @router.post(
        "/{session_id}/start",
        summary="Start a draft session: its comments now reserve stock",
        responses=documented(
            {401: UNAUTHORIZED, 404: NOT_FOUND, 409: "The session has ended already"}
        ),
    )
    async def start_live_session(seller: CurrentSeller, session_id: UUID) -> LiveSessionResponse:
        return LiveSessionResponse.of((await sessions.start(seller.id, session_id)).session)

    @router.post(
        "/{session_id}/end",
        summary="End a live session: it reserves nothing more and becomes read-only",
        responses=documented(
            {401: UNAUTHORIZED, 404: NOT_FOUND, 409: "The session has not started"}
        ),
    )
    async def end_live_session(seller: CurrentSeller, session_id: UUID) -> LiveSessionResponse:
        return LiveSessionResponse.of((await sessions.end(seller.id, session_id)).session)

    return router
