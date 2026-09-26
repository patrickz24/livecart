"""The product routes over the in-memory fakes: codes read by the domain, catalog rules, pages."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

import httpx2
import pytest

from livecart_api.testing import make_app, open_client, seller_headers
from livecart_core.application.dto import LiveSessionRecord
from livecart_core.domain.live_sessions import MAX_PRODUCTS_PER_SESSION
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.testing.fakes import InMemoryDatabase

# A12 as a Japanese or Chinese keyboard types it.
FULLWIDTH_A12 = chr(0xFF21) + chr(0xFF11) + chr(0xFF12)
NEW = {"code": "A12", "variant": "M", "name": "Robe fleurie", "price_minor": 2490, "stock_total": 5}


@dataclass
class Api:
    client: httpx2.AsyncClient
    database: InMemoryDatabase
    headers: dict[str, str]
    session: LiveSessionRecord

    def path(self, session_id: UUID | None = None) -> str:
        return f"/api/v1/live-sessions/{session_id or self.session.id}/products"

    async def add(self, **overrides: object) -> httpx2.Response:
        return await self.client.post(self.path(), json={**NEW, **overrides}, headers=self.headers)

    async def added(self, **overrides: object) -> dict[str, Any]:
        response = await self.add(**overrides)
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    async def patch(self, product_id: str, body: object) -> httpx2.Response:
        return await self.client.patch(
            f"{self.path()}/{product_id}", json=body, headers=self.headers
        )

    def set_status(self, status: LiveSessionStatus) -> None:
        """Move the session directly: the lifecycle routes have tests of their own."""
        sessions = self.database.tables.sessions
        sessions[self.session.id] = replace(sessions[self.session.id], status=status)


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    database = InMemoryDatabase()
    app = make_app(database=database)
    seller = database.add_seller()
    session = database.add_live_session(seller_id=seller.id, status=LiveSessionStatus.DRAFT)
    async with open_client(app) as client:
        yield Api(client, database, seller_headers(app, seller.id), session)


async def test_an_added_product_answers_its_catalog_row_and_stock(api: Api) -> None:
    response = await api.add()

    body = response.json()
    assert response.status_code == 201
    assert response.headers["Location"] == f"{api.path()}/{body['id']}"
    assert body == {
        "id": body["id"],
        "session_id": str(api.session.id),
        "code": "A12",
        "variant": "M",
        "name": "Robe fleurie",
        "price_minor": 2490,
        "currency": "EUR",
        "stock_total": 5,
        "reserved": 0,
        "sold": 0,
        "available": 5,
    }
    read = await api.client.get(response.headers["Location"], headers=api.headers)
    assert read.json() == body


@pytest.mark.parametrize(
    ("typed", "code"),
    [("a12", "A12"), (" A12 ", "A12"), (FULLWIDTH_A12, "A12"), ("xyz1234", "XYZ1234")],
)
async def test_a_code_is_read_by_the_domain(api: Api, typed: str, code: str) -> None:
    assert (await api.added(code=typed, variant=""))["code"] == code


async def test_a_variant_is_folded_and_may_be_left_out(api: Api) -> None:
    folded = await api.added(variant=" Bleu nuit ")
    without = await api.client.post(
        api.path(),
        json={"code": "B07", "name": "Sac", "price_minor": 100, "stock_total": 1},
        headers=api.headers,
    )

    assert folded["variant"] == "Bleu nuit"
    assert without.json()["variant"] == ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"code": "12A"},
        {"code": "ABCD1"},
        {"code": "A12345"},
        {"code": ""},
        {"variant": "x" * 33},
        {"name": " "},
        {"price_minor": -1},
        {"price_minor": 100_000_001},
        {"price_minor": 12.5},
        {"stock_total": -1},
        {"stock_total": 1_000_001},
        {"reserved": 1},
        {"currency": "USD"},
    ],
)
async def test_a_bad_or_unknown_field_is_refused(api: Api, overrides: dict[str, object]) -> None:
    response = await api.add(**overrides)

    assert response.status_code == 422
    assert api.database.tables.products == {}


async def test_the_domains_reason_explains_a_refused_code(api: Api) -> None:
    response = await api.add(code="12A")

    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "code"]
    assert "is not a product code" in error["msg"]


@pytest.mark.parametrize(
    ("first", "second", "detail"),
    [
        ("M", "M", "The session already sells this code and variant"),
        ("", "M", "A code has either one product without a variant or only products with one"),
    ],
)
async def test_the_catalog_refuses_a_taken_key_and_a_variant_mix(
    api: Api, first: str, second: str, detail: str
) -> None:
    await api.added(variant=first)

    response = await api.add(variant=second)

    assert response.status_code == 409
    assert response.json() == {"detail": detail}


async def test_a_full_catalog_refuses_one_more_product(api: Api) -> None:
    for number in range(MAX_PRODUCTS_PER_SESSION):
        api.database.add_product(api.session.id, f"Z{number}")

    response = await api.add()

    assert response.status_code == 409
    assert response.json() == {"detail": "The session holds as many products as it may"}


async def test_an_ended_session_takes_no_product(api: Api) -> None:
    api.set_status(LiveSessionStatus.ENDED)

    response = await api.add()

    assert response.status_code == 409
    assert response.json() == {"detail": "The session is ended: it can no longer change"}


async def test_the_products_come_in_creation_order_a_page_at_a_time(api: Api) -> None:
    added = [await api.added(code=f"B{n}", variant="") for n in range(3)]

    first = (await api.client.get(api.path(), params={"limit": 2}, headers=api.headers)).json()
    last = (
        await api.client.get(
            api.path(), params={"limit": 2, "cursor": first["next_cursor"]}, headers=api.headers
        )
    ).json()

    assert [item["id"] for item in first["items"]] == [added[0]["id"], added[1]["id"]]
    assert ([item["id"] for item in last["items"]], last["next_cursor"]) == (
        [added[2]["id"]],
        None,
    )
    too_long = await api.client.get(api.path(), params={"limit": 101}, headers=api.headers)
    assert too_long.status_code == 422


async def test_a_draft_product_changes_its_code_variant_name_and_price(api: Api) -> None:
    product = await api.added()

    response = await api.patch(
        product["id"], {"code": "a13", "variant": "L", "name": "Robe longue", "price_minor": 3990}
    )

    body = response.json()
    assert response.status_code == 200
    assert (body["code"], body["variant"], body["name"], body["price_minor"]) == (
        "A13",
        "L",
        "Robe longue",
        3990,
    )
    assert body["stock_total"] == product["stock_total"]


async def test_a_live_product_changes_its_price_but_not_its_code(api: Api) -> None:
    product = await api.added()
    api.set_status(LiveSessionStatus.LIVE)

    repriced = await api.patch(product["id"], {"price_minor": 1990, "code": "A12"})
    moved = await api.patch(product["id"], {"code": "A13"})

    assert (repriced.status_code, repriced.json()["price_minor"]) == (200, 1990)
    assert moved.status_code == 409


@pytest.mark.parametrize(
    "body", [{"stock_total": 50}, {"reserved": 0}, {"price_minor": -5}, {"code": "?"}]
)
async def test_the_stock_and_bad_values_are_not_changed_by_a_patch(
    api: Api, body: dict[str, object]
) -> None:
    product = await api.added()

    response = await api.patch(product["id"], body)

    assert response.status_code == 422


async def test_a_product_is_found_under_its_own_session_only(api: Api) -> None:
    product = await api.added()
    sibling = api.database.add_live_session(
        seller_id=api.session.seller_id, external_live_id="live-2"
    )

    elsewhere = await api.client.get(f"{api.path(sibling.id)}/{product['id']}", headers=api.headers)
    unknown = await api.client.get(f"{api.path()}/{UUID(int=404)}", headers=api.headers)
    no_session = await api.client.get(api.path(UUID(int=404)), headers=api.headers)

    assert [r.status_code for r in (elsewhere, unknown, no_session)] == [404, 404, 404]
    assert elsewhere.json() == unknown.json() == no_session.json() == {"detail": "Not found"}
