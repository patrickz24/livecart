"""``LiveSessionRepository`` on every backend: the in-memory fake, then PostgreSQL.

A seller reaches only their own sessions: every seller-scoped method answers
another seller's session exactly as a session that does not exist.
"""

from datetime import timedelta
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    LiveSessionChanges,
    LiveSessionRecord,
    NewLiveSession,
    Platform,
)
from livecart_core.application.ports import LiveSessionConflict, RecordNotFound
from livecart_core.domain.catalog import Catalog
from livecart_core.domain.errors import EditRefused, InvalidTransition
from livecart_core.domain.state_machines import LiveSessionStatus, TransitionOutcome
from livecart_core.testing.backends import ReservationBackend
from livecart_core.testing.builders import demo_catalog
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
LATER = NOW + timedelta(minutes=30)
UNKNOWN = UUID(int=404)
LIVE = LiveSessionStatus.LIVE
ENDED = LiveSessionStatus.ENDED


def _new(
    external_live_id: str = "fb-live-1", platform: Platform = Platform.FACEBOOK
) -> NewLiveSession:
    return NewLiveSession(title="Friday drop", platform=platform, external_live_id=external_live_id)


async def _draft(backend: ReservationBackend, seller_id: UUID | None = None) -> LiveSessionRecord:
    seller = seller_id if seller_id is not None else await backend.add_seller()
    return await backend.live_sessions.add(seller, _new(), now=NOW)


# --- Creation and reads ----------------------------------------------------------------


async def test_a_new_session_is_a_draft_of_its_seller(backend: ReservationBackend) -> None:
    seller_id = await backend.add_seller()

    session = await backend.live_sessions.add(seller_id, _new(), now=NOW)

    assert (session.seller_id, session.title, session.platform, session.external_live_id) == (
        seller_id,
        "Friday drop",
        Platform.FACEBOOK,
        "fb-live-1",
    )
    assert (session.status, session.created_at) == (LiveSessionStatus.DRAFT, NOW)
    assert (session.started_at, session.ended_at) == (None, None)
    assert await backend.live_sessions.get(session.id) == session
    assert await backend.live_sessions.get_for_seller(seller_id, session.id) == session


async def test_another_sellers_session_reads_as_missing(backend: ReservationBackend) -> None:
    session = await _draft(backend)
    other = await backend.add_seller()

    assert await backend.live_sessions.get_for_seller(other, session.id) is None
    assert await backend.live_sessions.get_for_seller(session.seller_id, UNKNOWN) is None
    assert await backend.live_sessions.get(UNKNOWN) is None


async def test_a_live_id_names_one_session_per_platform_whatever_its_seller(
    backend: ReservationBackend,
) -> None:
    await _draft(backend)
    other = await backend.add_seller()

    with pytest.raises(LiveSessionConflict):
        await backend.live_sessions.add(other, _new(), now=NOW)
    tiktok = await backend.live_sessions.add(other, _new(platform=Platform.TIKTOK), now=NOW)

    assert tiktok.external_live_id == "fb-live-1"


async def test_a_session_needs_an_existing_seller(backend: ReservationBackend) -> None:
    with pytest.raises(RecordNotFound):
        await backend.live_sessions.add(UNKNOWN, _new(), now=NOW)


async def test_a_seller_lists_their_own_sessions_newest_first_by_page(
    backend: ReservationBackend,
) -> None:
    seller_id = await backend.add_seller()
    other = await backend.add_seller()
    created = [
        await backend.live_sessions.add(seller_id, _new(f"live-{n}"), now=NOW) for n in range(5)
    ]
    await backend.live_sessions.add(other, _new("other-live"), now=NOW)
    sessions = backend.live_sessions

    first = await sessions.list_for_seller(seller_id, limit=2)
    second = await sessions.list_for_seller(seller_id, limit=2, cursor=first.next_cursor)
    last = await sessions.list_for_seller(seller_id, limit=2, cursor=second.next_cursor)

    newest_first = created[::-1]
    assert first.items == tuple(newest_first[:2])
    assert second.items == tuple(newest_first[2:4])
    assert last.items == (newest_first[4],)
    assert (first.next_cursor, second.next_cursor, last.next_cursor) == (
        newest_first[1].id,
        newest_first[3].id,
        None,
    )
    whole = await sessions.list_for_seller(seller_id, limit=5)
    assert (len(whole.items), whole.next_cursor) == (5, None)
    assert (await sessions.list_for_seller(UNKNOWN, limit=5)).items == ()


# --- Changes ---------------------------------------------------------------------------


async def test_a_draft_changes_its_title_platform_and_live_id(backend: ReservationBackend) -> None:
    session = await _draft(backend)
    changes = LiveSessionChanges(
        title="Saturday drop", platform=Platform.INSTAGRAM, external_live_id="ig-9"
    )

    updated = await backend.live_sessions.update(session.seller_id, session.id, changes)

    assert (updated.title, updated.platform, updated.external_live_id) == (
        "Saturday drop",
        Platform.INSTAGRAM,
        "ig-9",
    )
    assert updated.status is LiveSessionStatus.DRAFT
    assert await backend.live_sessions.get(session.id) == updated
    assert await backend.live_sessions.find_by_live_id(Platform.INSTAGRAM, "ig-9") == updated
    unchanged = await backend.live_sessions.update(
        session.seller_id, session.id, LiveSessionChanges()
    )
    assert unchanged == updated


async def test_a_live_session_keeps_its_platform_and_live_id(backend: ReservationBackend) -> None:
    session = await _draft(backend)
    sessions = backend.live_sessions
    await sessions.set_status(session.seller_id, session.id, LIVE, now=NOW)

    renamed = await sessions.update(
        session.seller_id,
        session.id,
        # A form that sends the keys back unchanged does not move them.
        LiveSessionChanges(
            title="Renamed", platform=Platform.FACEBOOK, external_live_id="fb-live-1"
        ),
    )
    for changes in (
        LiveSessionChanges(external_live_id="fb-live-2"),
        LiveSessionChanges(platform=Platform.TIKTOK),
    ):
        with pytest.raises(EditRefused) as refused:
            await sessions.update(session.seller_id, session.id, changes)
        assert refused.value.keys

    assert renamed.title == "Renamed"
    assert await sessions.get(session.id) == renamed


async def test_an_ended_session_changes_no_more(backend: ReservationBackend) -> None:
    session = await _draft(backend)
    sessions = backend.live_sessions
    await sessions.set_status(session.seller_id, session.id, LIVE, now=NOW)
    ended = (await sessions.set_status(session.seller_id, session.id, ENDED, now=LATER)).session

    with pytest.raises(EditRefused) as refused:
        await sessions.update(session.seller_id, session.id, LiveSessionChanges(title="Late"))

    assert not refused.value.keys
    assert await sessions.get(session.id) == ended


async def test_a_change_to_a_taken_live_id_or_by_another_seller_is_refused(
    backend: ReservationBackend,
) -> None:
    session = await _draft(backend)
    taken = await backend.live_sessions.add(session.seller_id, _new("fb-live-2"), now=NOW)
    other = await backend.add_seller()
    sessions = backend.live_sessions

    with pytest.raises(LiveSessionConflict):
        await sessions.update(
            session.seller_id, session.id, LiveSessionChanges(external_live_id="fb-live-2")
        )
    with pytest.raises(RecordNotFound):
        await sessions.update(other, session.id, LiveSessionChanges(title="Stolen"))
    with pytest.raises(RecordNotFound):
        await sessions.update(session.seller_id, UNKNOWN, LiveSessionChanges(title="Nothing"))

    assert await sessions.get(session.id) == session
    assert await sessions.get(taken.id) == taken


# --- Lifecycle -------------------------------------------------------------------------


async def test_a_session_starts_then_ends_with_its_timestamps(backend: ReservationBackend) -> None:
    session = await _draft(backend)
    sessions = backend.live_sessions

    started = await sessions.set_status(session.seller_id, session.id, LIVE, now=NOW)
    again = await sessions.set_status(session.seller_id, session.id, LIVE, now=LATER)
    ended = await sessions.set_status(session.seller_id, session.id, ENDED, now=LATER)
    replayed = await sessions.set_status(session.seller_id, session.id, ENDED, now=LATER)

    assert [result.outcome for result in (started, again, ended, replayed)] == [
        TransitionOutcome.APPLIED,
        TransitionOutcome.NOOP,
        TransitionOutcome.APPLIED,
        TransitionOutcome.NOOP,
    ]
    assert (started.session.status, started.session.started_at) == (LIVE, NOW)
    assert again.session == started.session
    assert (ended.session.status, ended.session.started_at, ended.session.ended_at) == (
        ENDED,
        NOW,
        LATER,
    )
    assert await sessions.get(session.id) == ended.session == replayed.session


@pytest.mark.parametrize(
    ("status", "target"),
    [
        (LiveSessionStatus.DRAFT, ENDED),
        (ENDED, LIVE),
        (LIVE, LiveSessionStatus.DRAFT),
    ],
)
async def test_a_session_never_skips_nor_goes_back(
    backend: ReservationBackend, status: LiveSessionStatus, target: LiveSessionStatus
) -> None:
    session = await backend.add_live_session(status=status)

    with pytest.raises(InvalidTransition):
        await backend.live_sessions.set_status(session.seller_id, session.id, target, now=NOW)

    stored = await backend.live_sessions.get(session.id)
    assert stored is not None
    assert (stored.status, stored.started_at, stored.ended_at) == (status, None, None)


async def test_another_seller_can_neither_start_nor_end_a_session(
    backend: ReservationBackend,
) -> None:
    session = await _draft(backend)
    other = await backend.add_seller()

    for target in (LIVE, ENDED):
        with pytest.raises(RecordNotFound):
            await backend.live_sessions.set_status(other, session.id, target, now=NOW)

    assert await backend.live_sessions.get(session.id) == session


# --- What the worker reads -------------------------------------------------------------


async def test_a_session_is_found_by_its_platform_live_id(backend: ReservationBackend) -> None:
    session = await backend.add_live_session(external_live_id="live-9")
    sessions = backend.live_sessions

    assert await sessions.find_by_live_id(Platform.SIMULATOR, "live-9") == session
    assert await sessions.find_by_live_id(Platform.TIKTOK, "live-9") is None
    assert await sessions.find_by_live_id(Platform.SIMULATOR, "live-1") is None


async def test_the_catalog_reads_back_as_it_was_seeded(backend: ReservationBackend) -> None:
    session = await backend.add_live_session()
    for product in demo_catalog():
        for variant in product.variants or (None,):
            await backend.add_product(
                session.id,
                str(product.code),
                variant="" if variant is None else str(variant),
                name=product.name,
            )

    assert await backend.live_sessions.load_catalog(session.id) == demo_catalog()


async def test_a_catalog_holds_only_its_session_products(backend: ReservationBackend) -> None:
    session = await backend.add_live_session()
    other = await backend.add_live_session(external_live_id="live-2")
    await backend.add_product(other.id, "Z99")

    assert await backend.live_sessions.load_catalog(session.id) == Catalog(())
    with pytest.raises(RecordNotFound):
        await backend.live_sessions.load_catalog(UNKNOWN)


# --- What the storefront reads ---------------------------------------------------------


async def test_the_current_session_is_the_live_one_that_started_last(
    backend: ReservationBackend,
) -> None:
    seller_id = await backend.add_seller()
    sessions = backend.live_sessions
    first = await sessions.add(seller_id, _new("fb-1"), now=NOW)
    second = await sessions.add(seller_id, _new("tt-1", Platform.TIKTOK), now=NOW)
    await sessions.add(seller_id, _new("ig-1", Platform.INSTAGRAM), now=NOW)

    assert await sessions.find_current(seller_id) is None
    await sessions.set_status(seller_id, second.id, LIVE, now=NOW)
    await sessions.set_status(seller_id, first.id, LIVE, now=LATER)
    latest = await sessions.find_current(seller_id)
    await sessions.set_status(seller_id, first.id, ENDED, now=LATER)
    remaining = await sessions.find_current(seller_id)

    assert latest is not None
    assert (latest.id, latest.started_at) == (first.id, LATER)
    assert remaining is not None
    assert remaining.id == second.id
    assert await sessions.find_current(await backend.add_seller()) is None
