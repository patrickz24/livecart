"""The payment bridge page over the fakes: a button back to the app, and nothing else.

The provider sends the buyer's browser to it with the order's reference
only; the page answers HTML with a strict CSP, a link to the return URL of
the order's last attempt, and escapes whatever it shows.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

import httpx2
import pytest

from livecart_api.middleware.security_headers import API_CSP
from livecart_api.pages import page_csp
from livecart_api.testing import make_app, make_settings, open_client, open_order
from livecart_core.application.dto import OrderRecord, PaymentProviderName
from livecart_core.domain.state_machines import PaymentStatus
from livecart_core.settings import AppEnv
from livecart_core.testing.fakes import (
    FAKE_CLOCK_START,
    TEST_ORDER_TOKENS,
    FakeClock,
    InMemoryDatabase,
)

BRIDGE = "/api/v1/payments/return"
RETURN_URL = "livecart://payment-return"


@dataclass
class Bridge:
    client: httpx2.AsyncClient
    database: InMemoryDatabase
    order: OrderRecord

    async def open_attempt(self, return_url: str = RETURN_URL) -> None:
        opening = await self.database.payments.open_attempt(
            self.order.id,
            provider=PaymentProviderName.MOCK,
            return_url=return_url,
            now=FAKE_CLOCK_START,
            grace=timedelta(seconds=600),
            max_attempts=3,
        )
        assert opening.payment is not None

    async def visit(self, status: str, ref: str | None = None) -> httpx2.Response:
        params = {"status": status, "ref": ref if ref is not None else self.order.reference.value}
        return await self.client.get(BRIDGE, params=params)


async def _bridge(
    app_env: AppEnv = AppEnv.DEVELOPMENT, **settings: object
) -> AsyncIterator[Bridge]:
    database = InMemoryDatabase()
    session = database.add_live_session()
    database.add_product(session.id, "B07")
    order = await open_order(database, session.id)
    app = make_app(
        database=database, clock=FakeClock(), settings=make_settings(app_env, **settings)
    )
    async with open_client(app) as client:
        yield Bridge(client, database, order)


@pytest.fixture
async def bridge() -> AsyncIterator[Bridge]:
    async for opened in _bridge():
        yield opened


async def test_success_answers_a_page_whose_button_opens_the_app(bridge: Bridge) -> None:
    await bridge.open_attempt()
    reference = bridge.order.reference.value

    page = await bridge.visit("success")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Payment submitted" in page.text
    assert (
        f'<a class="button" href="livecart://payment-return?status=success&amp;reference='
        f'{reference}">Return to the app</a>'
    ) in page.text
    assert "<script" not in page.text
    assert "<form" not in page.text


async def test_cancel_answers_the_same_button_with_its_outcome(bridge: Bridge) -> None:
    await bridge.open_attempt()

    page = await bridge.visit("cancel")

    assert page.status_code == 200
    assert "Payment cancelled" in page.text
    assert "livecart://payment-return?status=cancel&amp;reference=" in page.text


async def test_the_page_has_a_strict_csp_and_is_never_cached(bridge: Bridge) -> None:
    await bridge.open_attempt()

    page = await bridge.visit("success")

    assert page.headers["content-security-policy"] == page_csp(form_action="'none'")
    assert page.headers["content-security-policy"].startswith("default-src 'none'; ")
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "no-referrer"


async def test_no_token_reaches_the_page_or_the_link(bridge: Bridge) -> None:
    await bridge.open_attempt()
    token = TEST_ORDER_TOKENS.token_for(bridge.order.id)

    page = await bridge.visit("success")

    assert token not in page.text
    assert token not in str(page.request.url)


async def test_the_reference_is_read_tolerantly_and_shown_canonical(bridge: Bridge) -> None:
    await bridge.open_attempt()
    typed = bridge.order.reference.value.lower().replace("-", "")

    page = await bridge.visit("success", typed)

    assert page.status_code == 200
    assert f"reference={bridge.order.reference.value}" in page.text
    assert typed not in page.text


@pytest.mark.parametrize(
    ("status", "ref"),
    [
        ("success", "LC-000000"),
        ("success", "<script>alert(1)</script>"),
        ('"><img src=x onerror=alert(1)>', None),
        ("paid", None),
        ("success", "x" * 33),
    ],
)
async def test_an_unknown_order_or_a_forged_link_gets_the_same_404_page(
    bridge: Bridge, status: str, ref: str | None
) -> None:
    await bridge.open_attempt()

    page = await bridge.visit(status, ref)

    assert page.status_code == 404
    assert "Unknown payment" in page.text
    assert "<script>" not in page.text
    assert "<img" not in page.text
    assert "href=" not in page.text


async def test_a_link_without_its_parameters_is_the_404_page(bridge: Bridge) -> None:
    answers = [
        await bridge.client.get(BRIDGE),
        await bridge.client.get(BRIDGE, params={"status": "success"}),
        await bridge.client.get(BRIDGE, params={"ref": bridge.order.reference.value}),
    ]

    assert [answer.status_code for answer in answers] == [404, 404, 404]


async def test_an_order_that_never_opened_a_checkout_has_no_bridge(bridge: Bridge) -> None:
    page = await bridge.visit("success")

    assert page.status_code == 404


async def test_the_page_escapes_the_stored_return_url(bridge: Bridge) -> None:
    # A return URL the checkout accepts: RFC 3986 allows ' and & in a path.
    await bridge.open_attempt("livecart://pay/it's&more")

    page = await bridge.visit("success")

    assert 'href="livecart://pay/it&#x27;s&amp;more?status=success&amp;reference=' in page.text
    assert "it's&more" not in page.text


async def test_the_link_follows_the_last_attempt(bridge: Bridge) -> None:
    await bridge.open_attempt("livecart://first")
    pending = await bridge.database.payments.get_pending(bridge.order.id)
    assert pending is not None
    await bridge.database.payments.set_status(pending.id, PaymentStatus.EXPIRED)
    await bridge.open_attempt("samplelive://second")

    page = await bridge.visit("success")

    assert "samplelive://second?status=success" in page.text
    assert "livecart://first" not in page.text


async def test_not_found_answers_count_against_the_address_like_lookups() -> None:
    async for bridge in _bridge(rate_limit_buyer_lookup_not_found_per_minute=2):
        answers = [await bridge.visit("success", "LC-000000") for _ in range(3)]
        lookup = await bridge.client.get(f"/api/v1/orders/{bridge.order.reference}")

    assert [answer.status_code for answer in answers] == [404, 404, 429]
    # The address is past its not-found answers, for the lookup as well.
    assert lookup.status_code == 429
    assert answers[-1].headers["content-security-policy"] == API_CSP


async def test_the_bridge_is_served_in_production_too() -> None:
    async for bridge in _bridge(AppEnv.PRODUCTION):
        await bridge.open_attempt()
        page = await bridge.visit("success")

    assert page.status_code == 200
