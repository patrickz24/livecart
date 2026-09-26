"""The live session routes over the in-memory fakes: bodies, pages, lifecycle and answers.

``tests/integration/test_catalog_postgres.py`` runs the main journeys on the
SQL adapters; ``test_seller_isolation.py`` checks every route against
another seller.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx2
import pytest

from livecart_api.testing import make_app, open_client, seller_headers
from livecart_core.testing.fakes import InMemoryDatabase

SESSIONS = "/api/v1/live-sessions"
NEW = {"title": "Friday drop", "platform": "tiktok", "external_live_id": "tt-live-1"}


@dataclass
class Api:
    client: httpx2.AsyncClient
    database: InMemoryDatabase
    headers: dict[str, str]

    async def create(self, **overrides: object) -> dict[str, Any]:
        response = await self.client.post(SESSIONS, json={**NEW, **overrides}, headers=self.headers)
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    async def post(self, path: str, body: object = None) -> httpx2.Response:
        return await self.client.post(path, json=body, headers=self.headers)

    async def patch(self, session_id: str, body: object) -> httpx2.Response:
        return await self.client.patch(f"{SESSIONS}/{session_id}", json=body, headers=self.headers)


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    database = InMemoryDatabase()
    app = make_app(database=database)
    headers = seller_headers(app, database.add_seller().id)
    async with open_client(app) as client:
        yield Api(client, database, headers)


# --- Creation --------------------------------------------------------------------------


async def test_a_created_session_is_a_draft_with_its_own_location(api: Api) -> None:
    response = await api.client.post(SESSIONS, json=NEW, headers=api.headers)

    body = response.json()
    assert response.status_code == 201
    assert response.headers["Location"] == f"{SESSIONS}/{body['id']}"
    assert set(body) == {
        "id",
        "title",
        "platform",
        "external_live_id",
        "status",
        "created_at",
        "started_at",
        "ended_at",
    }
    assert (body["title"], body["platform"], body["external_live_id"]) == (
        "Friday drop",
        "tiktok",
        "tt-live-1",
    )
    assert (body["status"], body["started_at"], body["ended_at"]) == ("draft", None, None)
    read = await api.client.get(response.headers["Location"], headers=api.headers)
    assert read.json() == body


async def test_the_labels_are_stripped(api: Api) -> None:
    body = await api.create(title="  Friday drop ", external_live_id=" tt-live-1 ")

    assert (body["title"], body["external_live_id"]) == ("Friday drop", "tt-live-1")


@pytest.mark.parametrize(
    "body",
    [
        {**NEW, "status": "live"},
        {**NEW, "seller_id": str(UUID(int=1))},
        {**NEW, "title": "   "},
        {**NEW, "title": "x" * 201},
        {**NEW, "platform": "youtube"},
        {**NEW, "external_live_id": "x" * 129},
        {"title": "Friday drop", "platform": "tiktok"},
    ],
    ids=[
        "status",
        "seller_id",
        "blank_title",
        "long_title",
        "platform",
        "long_live_id",
        "missing_live_id",
    ],
)
async def test_a_body_with_an_unknown_bad_or_missing_field_is_refused(
    api: Api, body: dict[str, object]
) -> None:
    response = await api.client.post(SESSIONS, json=body, headers=api.headers)

    assert response.status_code == 422
    assert api.database.tables.sessions == {}


async def test_a_taken_platform_and_live_id_is_a_conflict(api: Api) -> None:
    await api.create()

    response = await api.client.post(SESSIONS, json=NEW, headers=api.headers)
    elsewhere = await api.client.post(
        SESSIONS, json={**NEW, "platform": "instagram"}, headers=api.headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Another session has this platform and live id"}
    assert elsewhere.status_code == 201


async def test_every_session_route_needs_an_access_token(api: Api) -> None:
    session = await api.create()

    answers = [
        await api.client.post(SESSIONS, json=NEW),
        await api.client.get(SESSIONS),
        await api.client.get(f"{SESSIONS}/{session['id']}"),
        await api.client.post(f"{SESSIONS}/{session['id']}/start"),
        await api.client.get(SESSIONS, headers={"Authorization": "Bearer not-a-jwt"}),
    ]

    assert [answer.status_code for answer in answers] == [401] * 5
    assert all(answer.headers["WWW-Authenticate"].startswith("Bearer") for answer in answers)


# --- Pages -----------------------------------------------------------------------------


async def test_the_sessions_come_newest_first_a_bounded_page_at_a_time(api: Api) -> None:
    created = [await api.create(external_live_id=f"tt-{n}") for n in range(3)]

    first = (await api.client.get(SESSIONS, params={"limit": 2}, headers=api.headers)).json()
    cursor = first["next_cursor"]
    last = (
        await api.client.get(SESSIONS, params={"limit": 2, "cursor": cursor}, headers=api.headers)
    ).json()
    default = (await api.client.get(SESSIONS, headers=api.headers)).json()

    assert [item["id"] for item in first["items"]] == [created[2]["id"], created[1]["id"]]
    assert cursor == created[1]["id"]
    assert ([item["id"] for item in last["items"]], last["next_cursor"]) == (
        [created[0]["id"]],
        None,
    )
    assert (len(default["items"]), default["next_cursor"]) == (3, None)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"cursor": "not-an-id"}])
async def test_a_page_beyond_its_bounds_is_refused(api: Api, params: dict[str, str | int]) -> None:
    response = await api.client.get(SESSIONS, params=params, headers=api.headers)

    assert response.status_code == 422


# --- Changes and lifecycle -------------------------------------------------------------


async def test_a_draft_changes_its_title_platform_and_live_id(api: Api) -> None:
    session = await api.create()

    response = await api.patch(
        session["id"], {"title": "Big drop", "platform": "facebook", "external_live_id": "fb-1"}
    )
    kept = await api.patch(session["id"], {"title": None})

    assert response.status_code == 200
    body = response.json()
    assert (body["title"], body["platform"], body["external_live_id"]) == (
        "Big drop",
        "facebook",
        "fb-1",
    )
    assert kept.json() == body


@pytest.mark.parametrize(
    "body", [{"status": "live"}, {"started_at": "2026-09-26T12:00:00Z"}, {"title": ""}]
)
async def test_a_change_of_an_unknown_or_bad_field_is_refused(
    api: Api, body: dict[str, object]
) -> None:
    session = await api.create()

    response = await api.patch(session["id"], body)

    assert response.status_code == 422


async def test_a_session_goes_live_then_ends_and_each_step_may_be_repeated(api: Api) -> None:
    session = await api.create()
    path = f"{SESSIONS}/{session['id']}"

    started = await api.post(f"{path}/start")
    started_again = await api.post(f"{path}/start")
    ended = await api.post(f"{path}/end")
    ended_again = await api.post(f"{path}/end")

    assert [r.status_code for r in (started, started_again, ended, ended_again)] == [200] * 4
    assert started.json()["status"] == "live"
    assert started.json()["started_at"] is not None
    assert started_again.json() == started.json()
    assert ended.json()["status"] == "ended"
    assert ended.json()["started_at"] == started.json()["started_at"]
    assert ended.json()["ended_at"] is not None
    assert ended_again.json() == ended.json()


async def test_a_session_never_skips_nor_goes_back(api: Api) -> None:
    draft = await api.create()
    ended = await api.create(external_live_id="tt-2")
    await api.post(f"{SESSIONS}/{ended['id']}/start")
    await api.post(f"{SESSIONS}/{ended['id']}/end")

    skipped = await api.post(f"{SESSIONS}/{draft['id']}/end")
    back = await api.post(f"{SESSIONS}/{ended['id']}/start")

    assert (skipped.status_code, back.status_code) == (409, 409)
    assert skipped.json() == {"detail": "The session is draft: it cannot become ended"}
    assert back.json() == {"detail": "The session is ended: it cannot become live"}


async def test_a_live_session_keeps_its_live_id_and_an_ended_one_changes_no_more(
    api: Api,
) -> None:
    session = await api.create()
    await api.post(f"{SESSIONS}/{session['id']}/start")

    renamed = await api.patch(session["id"], {"title": "Renamed", "external_live_id": "tt-live-1"})
    moved = await api.patch(session["id"], {"external_live_id": "tt-live-2"})
    await api.post(f"{SESSIONS}/{session['id']}/end")
    late = await api.patch(session["id"], {"title": "Too late"})

    assert renamed.status_code == 200
    assert (moved.status_code, late.status_code) == (409, 409)
    assert moved.json() == {
        "detail": "The session is live: its platform, live id and product codes and variants "
        "can no longer change"
    }
    assert late.json() == {"detail": "The session is ended: it can no longer change"}


async def test_an_unknown_session_is_404_on_every_route(api: Api) -> None:
    unknown = f"{SESSIONS}/{UUID(int=404)}"

    answers = [
        await api.client.get(unknown, headers=api.headers),
        await api.patch(str(UUID(int=404)), {"title": "Nothing"}),
        await api.post(f"{unknown}/start"),
        await api.post(f"{unknown}/end"),
    ]

    assert [answer.status_code for answer in answers] == [404] * 4
    assert all(answer.json() == {"detail": "Not found"} for answer in answers)
