"""The live session and product routes on the SQL adapters and livecart_test.

The journey of a session from draft to ended, and the table of every seller
route that names an object, sent by another seller: on PostgreSQL the 404
comes from the ``seller_id`` filter of the queries themselves.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_api.app import create_app
from livecart_api.testing import (
    SELLER_OBJECT_ROUTES,
    SellerRoute,
    current_time_clock,
    make_settings,
    open_client,
    owner_answer,
    postgres_resources,
    seller_headers,
)
from livecart_core.application.dto import LiveSessionRecord, ProductRecord
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.testing.backends import PostgresBackend

pytestmark = pytest.mark.integration

SESSIONS = "/api/v1/live-sessions"
UNKNOWN = UUID(int=404)


@dataclass
class PostgresApi:
    app: FastAPI
    client: httpx2.AsyncClient
    backend: PostgresBackend
    owner_id: UUID
    owner: dict[str, str]
    intruder: dict[str, str]


@pytest.fixture
async def api(
    clean_database: None, app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> AsyncIterator[PostgresApi]:
    app = create_app(make_settings(), postgres_resources(app_engine, clock=current_time_clock()))
    backend = PostgresBackend(app_engine, migrator_engine)
    owner_id, intruder_id = await backend.add_seller(), await backend.add_seller()
    async with open_client(app) as client:
        yield PostgresApi(
            app,
            client,
            backend,
            owner_id,
            seller_headers(app, owner_id),
            seller_headers(app, intruder_id),
        )


async def test_a_session_goes_from_draft_to_ended_with_its_catalog(api: PostgresApi) -> None:
    created = await api.client.post(
        SESSIONS,
        json={"title": "Friday drop", "platform": "simulator", "external_live_id": "friday"},
        headers=api.owner,
    )
    session_path = f"{SESSIONS}/{created.json()['id']}"
    added = await api.client.post(
        f"{session_path}/products",
        json={"code": "a12", "variant": "M", "name": "Robe", "price_minor": 2490, "stock_total": 5},
        headers=api.owner,
    )
    product_path = f"{session_path}/products/{added.json()['id']}"
    mixed = await api.client.post(
        f"{session_path}/products",
        json={"code": "A12", "name": "Robe", "price_minor": 2490, "stock_total": 5},
        headers=api.owner,
    )
    started = await api.client.post(f"{session_path}/start", headers=api.owner)
    moved = await api.client.patch(product_path, json={"variant": "L"}, headers=api.owner)
    repriced = await api.client.patch(product_path, json={"price_minor": 1990}, headers=api.owner)
    ended = await api.client.post(f"{session_path}/end", headers=api.owner)
    frozen = await api.client.patch(session_path, json={"title": "Late"}, headers=api.owner)
    listed = await api.client.get(f"{session_path}/products", headers=api.owner)

    statuses = [
        response.status_code
        for response in (created, added, mixed, started, moved, repriced, ended, frozen, listed)
    ]
    assert statuses == [201, 201, 409, 200, 409, 200, 200, 409, 200]
    assert (added.json()["code"], added.json()["currency"]) == ("A12", "EUR")
    assert (started.json()["status"], ended.json()["status"]) == ("live", "ended")
    assert [item["price_minor"] for item in listed.json()["items"]] == [1990]
    stored = await api.backend.live_sessions.list_for_seller(api.owner_id, limit=5)
    assert [row.status for row in stored.items] == [LiveSessionStatus.ENDED]


async def _live_session_with_a_product(api: PostgresApi) -> tuple[LiveSessionRecord, ProductRecord]:
    session = await api.backend.add_live_session(seller_id=api.owner_id)
    return session, await api.backend.add_product(session.id, "A12")


async def _send(
    api: PostgresApi, route: SellerRoute, headers: dict[str, str], session: UUID, product: UUID
) -> httpx2.Response:
    return await api.client.request(
        route.method,
        route.url(session, product),
        json=route.body,
        headers={**route.headers, **headers},
    )


@pytest.mark.parametrize("route", SELLER_OBJECT_ROUTES, ids=lambda r: f"{r.method} {r.path}")
async def test_another_sellers_object_answers_like_an_unknown_id(
    api: PostgresApi, route: SellerRoute
) -> None:
    session, product = await _live_session_with_a_product(api)

    foreign = await _send(api, route, api.intruder, session.id, product.id)
    unknown = await _send(api, route, api.owner, UNKNOWN, UNKNOWN)

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json() == {"detail": "Not found"}
    assert await api.backend.live_sessions.get(session.id) == session
    assert await api.backend.products.list_for_session(session.id) == [product]


@pytest.mark.parametrize("route", SELLER_OBJECT_ROUTES, ids=lambda r: f"{r.method} {r.path}")
async def test_the_owners_request_on_the_same_route_succeeds(
    api: PostgresApi, route: SellerRoute
) -> None:
    session, product = await _live_session_with_a_product(api)

    status, body = await owner_answer(api.app, api.client, route, api.owner, session.id, product.id)

    assert status in (200, 201), body
