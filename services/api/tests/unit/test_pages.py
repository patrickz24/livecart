"""The HTML pages of the API: their strict CSP, their escaping, and where the mock's is mounted.

The mock's hosted page on a real Redis is in
``tests/integration/test_mock_payments_redis.py``.
"""

import base64
import hashlib
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from livecart_api.middleware.security_headers import API_CSP
from livecart_api.pages import STYLE, STYLE_HASH, amount_text, html_page, page_csp, text
from livecart_api.testing import make_app, open_client
from livecart_core.domain.money import Money
from livecart_core.infrastructure.payments.mock import MockPaymentProvider
from livecart_core.infrastructure.redis_client import create_redis_client
from livecart_core.settings import AppEnv
from livecart_core.testing.fakes import FakeClock, FakePaymentProvider
from livecart_core.testing.keys import TEST_MOCK_PAYMENT_WEBHOOK_SECRET

# A ref of the wrong shape: the page answers without reading Redis.
UNKNOWN_PAGE = "/api/v1/mock-payments/mock_cs_unknown"


@pytest.fixture
async def unreachable_redis() -> AsyncIterator[Redis]:
    """A client of a host that does not exist: the tests below never reach Redis."""
    client = create_redis_client(SecretStr("redis://redis.invalid:6379/0"))
    try:
        yield client
    finally:
        await client.aclose()


def _mock(redis: Redis) -> MockPaymentProvider:
    return MockPaymentProvider(
        redis,
        prefix="unit",
        secret=TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
        clock=FakeClock(),
        public_base_url="http://localhost:8080",
    )


def test_a_page_allows_its_one_stylesheet_by_its_hash_and_nothing_else() -> None:
    digest = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()

    page = html_page("Title", "<p>Body</p>")
    with_forms = html_page("Title", "<p>Body</p>", forms=True)

    assert digest == STYLE_HASH
    assert page.headers["content-security-policy"] == (
        f"default-src 'none'; style-src 'sha256-{digest}'; form-action 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    assert with_forms.headers["content-security-policy"] == page_csp(form_action="'self'")
    assert page.headers["cache-control"] == "no-store"
    assert f"<style>{STYLE}</style>" in bytes(page.body).decode()
    assert "<script" not in bytes(page.body).decode()


def test_every_markup_character_is_escaped_quotes_included() -> None:
    assert text("<script>alert('x')</script>") == (
        "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;"
    )
    assert text('" onmouseover="x') == "&quot; onmouseover=&quot;x"
    assert text("a&b") == "a&amp;b"
    title = bytes(html_page("<b>x</b>", "").body).decode()
    assert "<title>&lt;b&gt;x&lt;/b&gt;</title>" in title


def test_an_amount_shows_its_minor_units_as_a_decimal() -> None:
    assert amount_text(Money(2_500, "EUR")) == "25.00 EUR"
    assert amount_text(Money(5, "EUR")) == "0.05 EUR"


async def test_the_security_headers_keep_a_pages_own_csp(unreachable_redis: Redis) -> None:
    app = make_app(payment_provider=_mock(unreachable_redis))

    async with open_client(app) as client:
        page = await client.get(UNKNOWN_PAGE)
        json = await client.get("/api/v1/nothing-here")

    assert page.status_code == 404
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"] == page_csp(form_action="'none'")
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert json.headers["content-security-policy"] == API_CSP


@pytest.mark.parametrize(
    "ref", ["mock_cs_" + "0" * 39, "mock_cs_" + "G" * 40, "cs_test_1", "mock_cs_" + "0" * 40 + "0"]
)
async def test_a_ref_of_the_wrong_shape_names_no_session_and_reads_nothing(
    unreachable_redis: Redis, ref: str
) -> None:
    app = make_app(payment_provider=_mock(unreachable_redis))

    async with open_client(app) as client:
        shown = await client.get(f"/api/v1/mock-payments/{ref}")
        paid = await client.post(f"/api/v1/mock-payments/{ref}/pay")
        cancelled = await client.post(f"/api/v1/mock-payments/{ref}/cancel")

    # Redis is unreachable: any read would have answered 503.
    assert [shown.status_code, paid.status_code, cancelled.status_code] == [404, 404, 404]
    assert "Unknown test payment" in shown.text


async def test_the_mock_page_is_not_mounted_in_production_nor_for_another_provider(
    unreachable_redis: Redis,
) -> None:
    production = make_app(app_env=AppEnv.PRODUCTION, payment_provider=_mock(unreachable_redis))
    fake = make_app(payment_provider=FakePaymentProvider())

    for app in (production, fake):
        async with open_client(app) as client:
            response = await client.get(UNKNOWN_PAGE)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
        assert response.headers["content-security-policy"] == API_CSP


async def test_the_html_pages_stay_out_of_the_openapi_document(unreachable_redis: Redis) -> None:
    app = make_app(payment_provider=_mock(unreachable_redis))

    assert not [path for path in app.openapi()["paths"] if "mock-payments" in path]
