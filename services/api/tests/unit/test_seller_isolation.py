"""Another seller's objects answer 404 on every seller route, exactly as ids that name nothing.

``SELLER_OBJECT_ROUTES`` lists every route that names a session or a
product; the inventory test fails when a new seller route is missing from
it. ``tests/integration/test_catalog_postgres.py`` runs the same table on the
SQL adapters, whose queries carry the seller filter.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI

from livecart_api.testing import (
    SELLER_OBJECT_ROUTES,
    SELLER_OWN_ROUTES,
    SellerRoute,
    make_app,
    open_client,
    seller_headers,
    seller_routes,
)
from livecart_core.application.dto import LiveSessionRecord, ProductRecord
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.testing.fakes import InMemoryDatabase

UNKNOWN = UUID(int=404)


@dataclass
class Sellers:
    app: FastAPI
    client: httpx2.AsyncClient
    database: InMemoryDatabase
    owner: dict[str, str]
    intruder: dict[str, str]
    session: LiveSessionRecord
    product: ProductRecord


@pytest.fixture
async def sellers() -> AsyncIterator[Sellers]:
    database = InMemoryDatabase()
    app = make_app(database=database)
    owner, intruder = database.add_seller(), database.add_seller()
    # Live, so that every route of the table, start and end included, answers the owner 200.
    session = database.add_live_session(seller_id=owner.id, status=LiveSessionStatus.LIVE)
    product = database.add_product(session.id, "A12")
    async with open_client(app) as client:
        yield Sellers(
            app,
            client,
            database,
            seller_headers(app, owner.id),
            seller_headers(app, intruder.id),
            session,
            product,
        )


def test_the_table_holds_every_seller_route_that_names_an_object() -> None:
    listed = {(route.method, route.path) for route in SELLER_OBJECT_ROUTES}

    assert seller_routes(make_app()) == listed | SELLER_OWN_ROUTES


async def _send(
    client: httpx2.AsyncClient,
    route: SellerRoute,
    headers: dict[str, str],
    session_id: UUID,
    product_id: UUID,
) -> httpx2.Response:
    return await client.request(
        route.method,
        route.url(session_id, product_id),
        json=route.body,
        headers={**route.headers, **headers},
    )


@pytest.mark.parametrize("route", SELLER_OBJECT_ROUTES, ids=lambda r: f"{r.method} {r.path}")
async def test_another_sellers_object_answers_like_an_unknown_id(
    sellers: Sellers, route: SellerRoute
) -> None:
    tables = sellers.database.tables
    before = (dict(tables.sessions), dict(tables.products))

    foreign = await _send(
        sellers.client, route, sellers.intruder, sellers.session.id, sellers.product.id
    )
    unknown = await _send(sellers.client, route, sellers.owner, UNKNOWN, UNKNOWN)

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json() == {"detail": "Not found"}
    assert (tables.sessions, tables.products) == before


@pytest.mark.parametrize("route", SELLER_OBJECT_ROUTES, ids=lambda r: f"{r.method} {r.path}")
async def test_the_owners_request_on_the_same_route_succeeds(
    sellers: Sellers, route: SellerRoute
) -> None:
    # The bodies of the table pass validation: the 404s above are the seller filter's.
    response = await _send(
        sellers.client, route, sellers.owner, sellers.session.id, sellers.product.id
    )

    assert response.status_code in (200, 201), response.text


async def test_a_seller_lists_none_of_another_sellers_sessions(sellers: Sellers) -> None:
    response = await sellers.client.get("/api/v1/live-sessions", headers=sellers.intruder)

    assert response.json() == {"items": [], "next_cursor": None}
