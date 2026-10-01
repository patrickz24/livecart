"""The buyer's routes over the fakes: the public storefront, the two tiers of an order, tracking.

``tests/integration/test_buyer_postgres.py`` runs the same journeys on PostgreSQL.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import httpx2
import pytest

from livecart_api.testing import make_app, open_client, open_order
from livecart_core.application.dto import (
    CarrierName,
    LiveSessionRecord,
    OrderRecord,
    ShipmentRef,
    ShippingAddress,
)
from livecart_core.domain.reference import CROCKFORD_ALPHABET
from livecart_core.domain.state_machines import LiveSessionStatus, ShipmentStatus
from livecart_core.settings import AppEnv
from livecart_core.testing.fakes import FAKE_CLOCK_START, TEST_ORDER_TOKENS, InMemoryDatabase
from livecart_core.testing.randomness import SequenceRandomSource

STOREFRONT = "/api/v1/storefronts"
ORDERS = "/api/v1/orders"
NOT_FOUND = {"detail": "Not found"}
PUBLIC_FIELDS = {
    "reference",
    "status",
    "product",
    "quantity",
    "total_minor",
    "currency",
    "reserved_until",
}
DETAIL_FIELDS = PUBLIC_FIELDS | {"unit_price_minor", "shipping_address", "paid_at", "expired_at"}


def _index(reference: str) -> int:
    """The draw that gives ``reference``: its six symbols read in base 32."""
    index = 0
    for symbol in reference.removeprefix("LC-"):
        index = index * len(CROCKFORD_ALPHABET) + CROCKFORD_ALPHABET.index(symbol)
    return index


# The references the fake draws, in order: a 0 and a 1 in the first, for tolerant reading.
REFERENCES = ("LC-1K3F0Q", "LC-7K3F9Q")


@dataclass
class Buyers:
    client: httpx2.AsyncClient
    database: InMemoryDatabase
    session: LiveSessionRecord
    order_a: OrderRecord
    order_b: OrderRecord

    def bearer(self, order: OrderRecord) -> dict[str, str]:
        return {"Authorization": f"Bearer {TEST_ORDER_TOKENS.token_for(order.id)}"}


@pytest.fixture
async def buyers() -> AsyncIterator[Buyers]:
    database = InMemoryDatabase(
        random_source=SequenceRandomSource(_index(reference) for reference in REFERENCES)
    )
    seller = database.add_seller(slug="demo-seller", display_name="Demo shop")
    seeded = database.add_live_session(seller_id=seller.id)
    session = replace(seeded, started_at=FAKE_CLOCK_START)
    database.tables.sessions[session.id] = session
    database.add_product(session.id, "A12", variant="M", name="Robe fleurie", stock_total=5)
    database.add_product(session.id, "B07", name="Sac en cuir", stock_total=2)
    order_a = await open_order(database, session.id, "A12", variant="M", buyer="buyer-a")
    order_b = await open_order(database, session.id, "B07", buyer="buyer-b", quantity=2)
    async with open_client(make_app(database=database)) as client:
        yield Buyers(client, database, session, order_a, order_b)


# --- Storefront ------------------------------------------------------------------------


async def test_a_storefront_shows_the_live_session_and_its_stock_only(buyers: Buyers) -> None:
    response = await buyers.client.get(f"{STOREFRONT}/demo-seller")

    assert response.status_code == 200
    assert response.json() == {
        "slug": "demo-seller",
        "display_name": "Demo shop",
        "live_session": {
            "title": "Friday drop",
            "platform": "simulator",
            "started_at": "2026-09-26T12:00:00Z",
            "products": [
                {
                    "code": "A12",
                    "variant": "M",
                    "name": "Robe fleurie",
                    "price_minor": 1000,
                    "currency": "EUR",
                    "available": 4,
                },
                {
                    "code": "B07",
                    "variant": "",
                    "name": "Sac en cuir",
                    "price_minor": 1000,
                    "currency": "EUR",
                    "available": 0,
                },
            ],
        },
    }
    seller = next(iter(buyers.database.tables.sellers.values()))
    for private in (seller.email, str(seller.id), str(buyers.session.id), "live-1"):
        assert private not in response.text


async def test_a_seller_without_a_live_session_shows_an_empty_storefront(buyers: Buyers) -> None:
    seller = buyers.database.add_seller(slug="sample-shop", display_name="Sample")
    buyers.database.add_live_session(
        seller_id=seller.id, external_live_id="draft-1", status=LiveSessionStatus.DRAFT
    )

    response = await buyers.client.get(f"{STOREFRONT}/sample-shop")

    assert response.json() == {
        "slug": "sample-shop",
        "display_name": "Sample",
        "live_session": None,
    }


@pytest.mark.parametrize("slug", ["unknown-shop", "Demo-Seller", "demo_seller"])
async def test_an_unknown_slug_has_no_storefront(buyers: Buyers, slug: str) -> None:
    response = await buyers.client.get(f"{STOREFRONT}/{slug}")

    assert (response.status_code, response.json()) == (404, NOT_FOUND)


async def test_a_slug_longer_than_any_is_refused(buyers: Buyers) -> None:
    response = await buyers.client.get(f"{STOREFRONT}/{'x' * 65}")

    assert response.status_code == 422


# --- The reference alone ---------------------------------------------------------------


async def test_the_reference_alone_shows_the_public_fields_only(buyers: Buyers) -> None:
    response = await buyers.client.get(f"{ORDERS}/{buyers.order_a.reference}")

    body = response.json()
    assert response.status_code == 200
    assert set(body) == PUBLIC_FIELDS
    assert body == {
        "reference": "LC-1K3F0Q",
        "status": "pending_payment",
        "product": {"code": "A12", "variant": "M", "name": "Robe fleurie"},
        "quantity": 1,
        "total_minor": 1000,
        "currency": "EUR",
        "reserved_until": "2026-09-26T12:15:00Z",
    }
    for private in (
        "buyer-a",
        str(buyers.order_a.id),
        TEST_ORDER_TOKENS.token_for(buyers.order_a.id),
    ):
        assert private not in response.text


@pytest.mark.parametrize("typed", ["lc-1k3f0q", "LC1K3F0Q", "lc1k3foq", "LC-IK3FOQ", "LC-lk3f0q"])
async def test_a_reference_is_read_tolerantly(buyers: Buyers, typed: str) -> None:
    response = await buyers.client.get(f"{ORDERS}/{typed}")

    assert response.status_code == 200
    assert response.json()["reference"] == "LC-1K3F0Q"


@pytest.mark.parametrize("typed", ["LC-000000", "LC-UUUUUU", "LC-1K3F0", "order-1"])
async def test_an_unknown_or_malformed_reference_is_404(buyers: Buyers, typed: str) -> None:
    response = await buyers.client.get(f"{ORDERS}/{typed}")

    assert (response.status_code, response.json()) == (404, NOT_FOUND)


# --- The order's access token ----------------------------------------------------------


async def test_the_orders_token_opens_its_details(buyers: Buyers) -> None:
    path = f"{ORDERS}/{buyers.order_a.reference}/details"

    before = await buyers.client.get(path, headers=buyers.bearer(buyers.order_a))
    address = ShippingAddress(
        full_name="Ada Buyer",
        line1="1 rue du Live",
        postal_code="75001",
        city="Paris",
        country="FR",
    )
    await buyers.database.orders.set_shipping_address(buyers.order_a.id, address)
    after = await buyers.client.get(path, headers=buyers.bearer(buyers.order_a))
    public = await buyers.client.get(f"{ORDERS}/{buyers.order_a.reference}")

    assert before.status_code == after.status_code == 200
    assert set(before.json()) == DETAIL_FIELDS
    assert before.json()["shipping_address"] is None
    assert (before.json()["unit_price_minor"], before.json()["paid_at"]) == (1000, None)
    assert after.json()["shipping_address"] == address.model_dump()
    assert "Ada Buyer" not in public.text


async def test_a_missing_or_wrong_token_gets_the_404_of_an_unknown_reference(
    buyers: Buyers,
) -> None:
    path = f"{ORDERS}/{buyers.order_a.reference}/details"
    token = TEST_ORDER_TOKENS.token_for(buyers.order_a.id)
    wrong = ("B" if token[0] != "B" else "C") + token[1:]

    answers = [
        await buyers.client.get(path),
        await buyers.client.get(path, headers={"Authorization": f"Bearer {wrong}"}),
        await buyers.client.get(path, headers={"Authorization": "Bearer not-a-token"}),
        await buyers.client.get(path, headers={"Authorization": f"Basic {token}"}),
        await buyers.client.get(
            f"{ORDERS}/LC-000000/details", headers=buyers.bearer(buyers.order_a)
        ),
    ]

    assert [answer.status_code for answer in answers] == [404] * 5
    assert all(answer.json() == NOT_FOUND for answer in answers)
    assert all("WWW-Authenticate" not in answer.headers for answer in answers)


async def test_the_token_of_order_a_does_not_open_order_b(buyers: Buyers) -> None:
    opened_a = await buyers.client.get(
        f"{ORDERS}/{buyers.order_a.reference}/details", headers=buyers.bearer(buyers.order_a)
    )
    b_with_a = await buyers.client.get(
        f"{ORDERS}/{buyers.order_b.reference}/details", headers=buyers.bearer(buyers.order_a)
    )
    b_with_b = await buyers.client.get(
        f"{ORDERS}/{buyers.order_b.reference}/details", headers=buyers.bearer(buyers.order_b)
    )

    assert (opened_a.status_code, b_with_a.status_code, b_with_b.status_code) == (200, 404, 200)
    assert b_with_a.json() == NOT_FOUND
    assert b_with_b.json()["quantity"] == 2


def test_the_token_routes_declare_the_order_token_and_the_public_ones_none() -> None:
    paths = make_app(app_env=AppEnv.PRODUCTION).openapi()["paths"]

    assert paths["/api/v1/orders/{reference}/details"]["get"]["security"] == [
        {"OrderAccessToken": []}
    ]
    assert paths["/api/v1/orders/{reference}/tracking"]["get"]["security"] == [
        {"OrderAccessToken": []}
    ]
    assert "security" not in paths["/api/v1/orders/{reference}"]["get"]
    assert "security" not in paths["/api/v1/storefronts/{seller_slug}"]["get"]


async def test_an_expired_reservation_still_shows_its_status(buyers: Buyers) -> None:
    order = buyers.order_b
    await buyers.database.orders.expire_due(
        now=order.reserved_until + timedelta(seconds=1), limit=10
    )

    response = await buyers.client.get(f"{ORDERS}/{order.reference}")

    assert response.json()["status"] == "expired"


# --- Tracking --------------------------------------------------------------------------

TRACKING_FIELDS = {"reference", "carrier", "tracking_number", "status", "last_event_at"}


def _ref(created_at: datetime = FAKE_CLOCK_START) -> ShipmentRef:
    return ShipmentRef(
        CarrierName.MOCK, "mock_shp_" + "1" * 32, "MOCK-7K3F9Q2XABCD1234", created_at
    )


async def test_the_orders_token_tracks_its_parcel(buyers: Buyers) -> None:
    path = f"{ORDERS}/{buyers.order_a.reference.value.lower()}/tracking"
    added = await buyers.database.shipments.add(buyers.order_a.id, _ref())

    created = await buyers.client.get(path, headers=buyers.bearer(buyers.order_a))
    picked_up = FAKE_CLOCK_START + timedelta(minutes=2)
    await buyers.database.shipments.set_status(
        added.shipment.id, ShipmentStatus.IN_TRANSIT, event_at=picked_up
    )
    moving = await buyers.client.get(path, headers=buyers.bearer(buyers.order_a))
    public = await buyers.client.get(f"{ORDERS}/{buyers.order_a.reference}")

    assert created.status_code == moving.status_code == 200
    assert created.json() == {
        "reference": buyers.order_a.reference.value,
        "carrier": "mock",
        "tracking_number": "MOCK-7K3F9Q2XABCD1234",
        "status": "created",
        "last_event_at": "2026-09-26T12:00:00Z",
    }
    assert (moving.json()["status"], moving.json()["last_event_at"]) == (
        "in_transit",
        "2026-09-26T12:02:00Z",
    )
    assert set(moving.json()) == TRACKING_FIELDS
    # The reference alone never shows the parcel.
    assert "MOCK-" not in public.text


async def test_tracking_without_the_orders_own_token_is_the_404_of_an_unknown_reference(
    buyers: Buyers,
) -> None:
    await buyers.database.shipments.add(buyers.order_a.id, _ref())
    path = f"{ORDERS}/{buyers.order_a.reference}/tracking"
    token = TEST_ORDER_TOKENS.token_for(buyers.order_a.id)
    wrong = ("B" if token[0] != "B" else "C") + token[1:]

    answers = [
        await buyers.client.get(path),
        await buyers.client.get(path, headers={"Authorization": f"Bearer {wrong}"}),
        await buyers.client.get(path, headers=buyers.bearer(buyers.order_b)),
        await buyers.client.get(
            f"{ORDERS}/LC-000000/tracking", headers=buyers.bearer(buyers.order_a)
        ),
        # The right token, of an order that has no shipment yet.
        await buyers.client.get(
            f"{ORDERS}/{buyers.order_b.reference}/tracking", headers=buyers.bearer(buyers.order_b)
        ),
    ]

    assert [answer.status_code for answer in answers] == [404] * 5
    assert all(answer.json() == NOT_FOUND for answer in answers)
    assert all("MOCK-" not in answer.text for answer in answers)
