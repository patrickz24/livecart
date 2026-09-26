"""``SellerLiveSessions.start`` and ``end`` publish ``session.updated`` after the commit."""

from uuid import UUID

import pytest

from livecart_core.application.dto import NewLiveSession, Platform, SessionEventType
from livecart_core.application.live_sessions import SellerLiveSessions
from livecart_core.application.session_events import SessionPayload
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.state_machines import LiveSessionStatus, TransitionOutcome
from livecart_core.testing.fakes import FakeClock, FakeEventPublisher, InMemoryDatabase


async def _draft() -> tuple[SellerLiveSessions, FakeEventPublisher, FakeClock, UUID, UUID]:
    database, clock, events = InMemoryDatabase(), FakeClock(), FakeEventPublisher()
    sessions = SellerLiveSessions(database.live_sessions, clock, events)
    seller = database.add_seller()
    session = await sessions.create(
        seller.id, NewLiveSession("Friday drop", Platform.SIMULATOR, "friday")
    )
    return sessions, events, clock, seller.id, session.id


async def test_a_start_and_an_end_publish_the_session_as_it_stands() -> None:
    sessions, events, clock, seller_id, session_id = await _draft()

    await sessions.start(seller_id, session_id)
    clock.advance(60)
    ended = await sessions.end(seller_id, session_id)

    started_event, ended_event = events.of_type(SessionEventType.SESSION_UPDATED)
    assert started_event.session_id == ended_event.session_id == session_id
    assert started_event.correlation_id is None
    assert started_event.data["status"] == "live"
    assert ended_event.data == SessionPayload.of(ended.session).model_dump(mode="json")
    assert ended_event.data["status"] == "ended"
    assert ended_event.data["ended_at"] is not None
    assert ended_event.occurred_at == clock.now()
    assert sessions.unpublished_events == 0


async def test_a_move_already_made_publishes_nothing_again() -> None:
    sessions, events, _, seller_id, session_id = await _draft()
    await sessions.start(seller_id, session_id)

    again = await sessions.start(seller_id, session_id)

    assert again.outcome is TransitionOutcome.NOOP
    assert len(events.published) == 1


async def test_a_refused_move_publishes_nothing() -> None:
    sessions, events, _, seller_id, session_id = await _draft()

    with pytest.raises(InvalidTransition):
        await sessions.end(seller_id, session_id)

    assert events.published == []


async def test_a_failed_publication_is_counted_and_the_session_still_moves() -> None:
    sessions, events, _, seller_id, session_id = await _draft()
    events.fail_next()

    started = await sessions.start(seller_id, session_id)

    assert started.outcome is TransitionOutcome.APPLIED
    assert started.session.status is LiveSessionStatus.LIVE
    assert events.published == []
    assert sessions.unpublished_events == 1
