"""A seller's live sessions: create, list, read and edit them, then start and end them.

Every method takes the id of the seller the access token names, and every
repository call it makes filters by that id. A session of another seller is
therefore ``RecordNotFound``, the very answer a session that does not exist
gets, which the API turns into 404 (never 403, which would confirm that the
id exists). A session is a draft when created; ``start`` and ``end`` move it
along ``draft -> live -> ended``, and what it still takes in each status is
the rule of ``livecart_core.domain.live_sessions``.
"""

from uuid import UUID

from livecart_core.application.dto import (
    LiveSessionChanges,
    LiveSessionRecord,
    LiveSessionTransition,
    NewLiveSession,
    Page,
    check_page_size,
)
from livecart_core.application.ports import Clock, LiveSessionRepository, RecordNotFound
from livecart_core.domain.state_machines import LiveSessionStatus


class SellerLiveSessions:
    """The live session operations of one seller at a time; see the module docstring."""

    def __init__(self, sessions: LiveSessionRepository, clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock

    async def create(self, seller_id: UUID, session: NewLiveSession) -> LiveSessionRecord:
        """A new draft; ``LiveSessionConflict`` when its platform and live id are taken."""
        return await self._sessions.add(seller_id, session, now=self._clock.now())

    async def list(
        self, seller_id: UUID, *, limit: int, cursor: UUID | None = None
    ) -> Page[LiveSessionRecord]:
        """The seller's sessions, newest first, a page of ``limit`` at a time."""
        return await self._sessions.list_for_seller(
            seller_id, limit=check_page_size(limit), cursor=cursor
        )

    async def get(self, seller_id: UUID, session_id: UUID) -> LiveSessionRecord:
        session = await self._sessions.get_for_seller(seller_id, session_id)
        if session is None:
            raise RecordNotFound(f"no live session {session_id} for this seller")
        return session

    async def update(
        self, seller_id: UUID, session_id: UUID, changes: LiveSessionChanges
    ) -> LiveSessionRecord:
        """Change the title, and in a draft the platform and live id too."""
        return await self._sessions.update(seller_id, session_id, changes)

    async def start(self, seller_id: UUID, session_id: UUID) -> LiveSessionTransition:
        """``draft -> live``: comments of the session now reserve stock."""
        return await self._sessions.set_status(
            seller_id, session_id, LiveSessionStatus.LIVE, now=self._clock.now()
        )

    async def end(self, seller_id: UUID, session_id: UUID) -> LiveSessionTransition:
        """``live -> ended``: the session reserves nothing more and becomes read-only."""
        return await self._sessions.set_status(
            seller_id, session_id, LiveSessionStatus.ENDED, now=self._clock.now()
        )
