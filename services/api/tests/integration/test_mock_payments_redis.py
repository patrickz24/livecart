"""The mock provider's hosted page on a real Redis: Pay, Cancel, and the webhook it posts.

The webhook goes through a fake transport that records it: the payment
webhook route itself is tested with the registry it records into.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from uuid import UUID

import httpx2
import pytest
from redis.asyncio import Redis

from livecart_api.pages import page_csp
from livecart_api.testing import current_time_clock, make_app, make_settings, open_client
from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutRequest,
    CheckoutSession,
    CheckoutStatus,
)
from livecart_core.domain.money import Money
from livecart_core.domain.reference import OrderReference
from livecart_core.infrastructure.payments.mock import MockPaymentProvider
from livecart_core.testing.fakes import FakeClock
from livecart_core.testing.keys import TEST_MOCK_PAYMENT_WEBHOOK_SECRET

pytestmark = pytest.mark.integration

WEBHOOK_URL = "http://127.0.0.1:8000/api/v1/webhooks/payments/mock"
BRIDGE = "http://localhost:8080/api/v1/payments/return"
CHECKOUT = CheckoutRequest(
    UUID(int=10),
    1,
    OrderReference("LC-7K3F9Q"),
    Money(2_500, "EUR"),
    f"{BRIDGE}?status=success&ref=LC-7K3F9Q",
    f"{BRIDGE}?status=cancel&ref=LC-7K3F9Q",
)


@dataclass
class Webhooks:
    """What the hosted page posted, and what the payment webhook answers it."""

    status_code: int = 200
    # A webhook nobody listens to: the connection is refused.
    down: bool = False
    received: list[httpx2.Request] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.down:
            raise httpx2.ConnectError("connection refused", request=request)
        self.received.append(request)
        return httpx2.Response(self.status_code, json={"received": True})


@dataclass
class HostedPage:
    client: httpx2.AsyncClient
    mock: MockPaymentProvider
    webhooks: Webhooks
    clock: FakeClock

    async def open(self, request: CheckoutRequest = CHECKOUT) -> CheckoutSession:
        return await self.mock.create_checkout(request)


@pytest.fixture
async def hosted(redis_client: Redis, redis_key_prefix: str) -> AsyncIterator[HostedPage]:
    clock = current_time_clock()
    mock = MockPaymentProvider(
        redis_client,
        prefix=redis_key_prefix,
        secret=TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
        clock=clock,
        public_base_url="http://localhost:8080",
    )
    webhooks = Webhooks()
    app = make_app(
        clock=clock,
        settings=make_settings(mock_payment_webhook_url=WEBHOOK_URL),
        payment_provider=mock,
        mock_webhook_transport=httpx2.MockTransport(webhooks.handle),
    )
    async with open_client(app) as client:
        yield HostedPage(client, mock, webhooks, clock)


def _path(session: CheckoutSession) -> str:
    return session.redirect_url.removeprefix("http://localhost:8080")


async def test_the_page_shows_the_order_and_its_amount_with_pay_and_cancel(
    hosted: HostedPage,
) -> None:
    session = await hosted.open()

    page = await hosted.client.get(_path(session))

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"] == page_csp(form_action="'self'")
    assert "LC-7K3F9Q" in page.text
    assert "25.00 EUR" in page.text
    assert f'action="/api/v1/mock-payments/{session.provider_ref}/pay"' in page.text
    assert f'action="/api/v1/mock-payments/{session.provider_ref}/cancel"' in page.text
    # The page never carries the key that signs the webhook.
    assert TEST_MOCK_PAYMENT_WEBHOOK_SECRET.get_secret_value() not in page.text


async def test_pay_completes_the_session_posts_the_signed_webhook_and_goes_to_success(
    hosted: HostedPage,
) -> None:
    session = await hosted.open()

    paid = await hosted.client.post(f"{_path(session)}/pay")

    assert paid.status_code == 303
    assert paid.headers["location"] == CHECKOUT.success_url
    completed = await hosted.mock.get_checkout(session.provider_ref)
    assert completed.status is CheckoutStatus.COMPLETE
    [webhook] = hosted.webhooks.received
    assert (webhook.method, str(webhook.url)) == ("POST", WEBHOOK_URL)
    assert webhook.headers["content-type"] == "application/json"
    event = await hosted.mock.verify_webhook(dict(webhook.headers), webhook.content)
    assert event.event_type == CheckoutEventType.COMPLETED.value
    assert (event.checkout_ref, event.payment_ref) == (
        session.provider_ref,
        completed.payment_ref,
    )
    assert (event.amount_minor, event.currency) == (2_500, "EUR")


async def test_paying_twice_posts_the_same_event_again(hosted: HostedPage) -> None:
    session = await hosted.open()

    await hosted.client.post(f"{_path(session)}/pay")
    again = await hosted.client.post(f"{_path(session)}/pay")
    shown = await hosted.client.get(_path(session))

    assert again.status_code == 303
    first, second = hosted.webhooks.received
    events = [
        await hosted.mock.verify_webhook(dict(request.headers), request.content)
        for request in (first, second)
    ]
    assert events[0] == events[1]
    assert "This test payment is complete" in shown.text
    assert '/pay"' not in shown.text


async def test_a_session_expired_first_cannot_be_paid_and_posts_nothing(
    hosted: HostedPage,
) -> None:
    session = await hosted.open()
    await hosted.mock.expire_checkout(session.provider_ref)

    paid = await hosted.client.post(f"{_path(session)}/pay")

    assert paid.status_code == 409
    assert "has expired" in paid.text
    assert hosted.webhooks.received == []
    assert (await hosted.mock.get_checkout(session.provider_ref)).status is CheckoutStatus.EXPIRED


async def test_cancel_goes_back_and_leaves_the_session_open(hosted: HostedPage) -> None:
    session = await hosted.open()

    cancelled = await hosted.client.post(f"{_path(session)}/cancel")

    assert cancelled.status_code == 303
    assert cancelled.headers["location"] == CHECKOUT.cancel_url
    assert (await hosted.mock.get_checkout(session.provider_ref)).status is CheckoutStatus.OPEN
    assert hosted.webhooks.received == []


@pytest.mark.parametrize(
    ("status_code", "down", "logged"),
    [(404, False, "mock_payment_webhook_refused"), (200, True, "mock_payment_webhook_failed")],
)
async def test_a_webhook_refused_or_unreachable_still_sends_the_buyer_on(
    hosted: HostedPage,
    capsys: pytest.CaptureFixture[str],
    status_code: int,
    down: bool,
    logged: str,
) -> None:
    hosted.webhooks.status_code, hosted.webhooks.down = status_code, down
    session = await hosted.open()

    paid = await hosted.client.post(f"{_path(session)}/pay")

    # The session is complete at the provider: the sweep reconciles from it.
    assert paid.status_code == 303
    assert (await hosted.mock.get_checkout(session.provider_ref)).status is (
        CheckoutStatus.COMPLETE
    )
    assert logged in capsys.readouterr().out


async def test_an_unknown_session_has_no_page(hosted: HostedPage) -> None:
    missing = "/api/v1/mock-payments/mock_cs_" + "0" * 40

    answers = [
        await hosted.client.get(missing),
        await hosted.client.post(f"{missing}/pay"),
        await hosted.client.post(f"{missing}/cancel"),
    ]

    assert [answer.status_code for answer in answers] == [404, 404, 404]
    assert hosted.webhooks.received == []


async def test_the_page_escapes_what_it_shows(hosted: HostedPage) -> None:
    hostile = replace(CHECKOUT, success_url=f'{BRIDGE}?status=success&ref="><script>x</script>')
    session = await hosted.open(hostile)
    await hosted.mock.pay(session.provider_ref)

    shown = await hosted.client.get(_path(session))

    assert "<script>" not in shown.text
    assert "&quot;&gt;&lt;script&gt;x&lt;/script&gt;" in shown.text
    assert "status=success&amp;ref=" in shown.text
