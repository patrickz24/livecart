"""The mock provider's hosted page: what a buyer sees instead of Stripe Checkout in development.

``POST /api/v1/orders/{reference}/checkout`` sends the buyer's browser to
``{PUBLIC_API_BASE_URL}/api/v1/mock-payments/{ref}`` while
``PAYMENT_PROVIDER=mock``, the default. The page shows the order's reference
and amount, marked as a test, with two buttons:

- **Pay** posts to ``/{ref}/pay``: the mock settles the session as complete
  (unless LiveCart expired it first), then the API signs the
  ``checkout.session.completed`` event with ``MOCK_PAYMENT_WEBHOOK_SECRET``
  and posts it to ``MOCK_PAYMENT_WEBHOOK_URL``, its own payment webhook, as
  a provider would. The page never holds the key. The buyer is then sent to
  the session's success URL, the bridge page. A delivery that fails is
  logged and not retried here: the session is complete at the provider all
  the same, and the expiry sweep reconciles an order from the session it
  reads back. Paying again posts the same event again, which the webhook
  registry counts once;
- **Cancel** posts to ``/{ref}/cancel`` and sends the buyer to the cancel
  URL, leaving the session open, as Stripe's back link does.

The routes exist only outside production, and only while the mock is the
provider: ``create_app`` does not mount them otherwise. They are pages for a
browser, outside the OpenAPI document (``livecart_api.pages``).
"""

import re
from typing import Annotated, Final

import httpx2
import structlog
from fastapi import APIRouter, Path, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from livecart_api.pages import amount_text, html_page, text
from livecart_core.application.dto import CheckoutStatus, PaymentEvent
from livecart_core.application.ports import RecordNotFound
from livecart_core.infrastructure.payments.mock import (
    MOCK_PAYMENTS_PATH,
    SESSION_REF_PREFIX,
    MockHostedSession,
    MockPaymentProvider,
)

logger = structlog.get_logger(__name__)

# What a session ref looks like; anything else names no session.
SESSION_REF_PATTERN: Final = re.compile(rf"{SESSION_REF_PREFIX}[0-9a-f]{{40}}")
WEBHOOK_TIMEOUT_S: Final = 5.0

SessionRef = Annotated[str, Path(max_length=64)]


class MockWebhookSender:
    """Signs a mock event and posts it to the payment webhook, once; see the module docstring.

    ``transport`` replaces the network in the tests.
    """

    def __init__(
        self,
        mock: MockPaymentProvider,
        url: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = WEBHOOK_TIMEOUT_S,
    ) -> None:
        self._mock = mock
        self._url = url
        self._transport = transport
        self._timeout_s = timeout_s

    async def deliver(self, event: PaymentEvent) -> bool:
        """Whether the webhook answered 2xx; any failure is logged, by its type or status only."""
        headers, body = self._mock.signed_webhook(event)
        try:
            async with httpx2.AsyncClient(
                transport=self._transport, timeout=self._timeout_s
            ) as client:
                response = await client.post(
                    self._url,
                    content=body,
                    headers={**headers, "Content-Type": "application/json"},
                )
        except httpx2.HTTPError as error:
            logger.warning(
                "mock_payment_webhook_failed", error=type(error).__name__, event_id=event.event_id
            )
            return False
        if not response.is_success:
            logger.warning(
                "mock_payment_webhook_refused",
                status_code=response.status_code,
                event_id=event.event_id,
            )
            return False
        logger.info("mock_payment_webhook_delivered", event_id=event.event_id)
        return True


def _unknown() -> HTMLResponse:
    return html_page(
        "Unknown test payment",
        "<h1>Unknown test payment</h1><p>No test payment has this link.</p>",
        status_code=status.HTTP_404_NOT_FOUND,
    )


def _hosted_page(hosted: MockHostedSession) -> HTMLResponse:
    session = hosted.session
    ref = text(session.provider_ref)
    amount = text(amount_text(session.amount))
    summary = (
        "<h1>LiveCart test payment</h1>"
        '<p class="note">Test mode: no card, no money. This page stands for the '
        "payment provider's hosted checkout.</p>"
        f"<p>Order <strong>{text(hosted.reference.value)}</strong><br>"
        f"Amount <strong>{amount}</strong></p>"
    )
    if session.status is CheckoutStatus.OPEN:
        actions = (
            f'<form method="post" action="{MOCK_PAYMENTS_PATH}/{ref}/pay">'
            f'<button class="button" type="submit">Pay {amount}</button></form>'
            f'<form method="post" action="{MOCK_PAYMENTS_PATH}/{ref}/cancel">'
            '<button class="button secondary" type="submit">Cancel</button></form>'
        )
    elif session.status is CheckoutStatus.COMPLETE:
        actions = (
            "<p>This test payment is complete.</p>"
            f'<a class="button" href="{text(hosted.success_url)}">Continue</a>'
        )
    else:
        actions = (
            "<p>This test payment has expired: the reservation was released.</p>"
            f'<a class="button secondary" href="{text(hosted.cancel_url)}">Continue</a>'
        )
    return html_page("LiveCart test payment", summary + actions, forms=True)


def build_mock_payment_router(mock: MockPaymentProvider, sender: MockWebhookSender) -> APIRouter:
    router = APIRouter(prefix=MOCK_PAYMENTS_PATH, include_in_schema=False)

    async def find(ref: str) -> MockHostedSession | None:
        if not SESSION_REF_PATTERN.fullmatch(ref):
            return None
        try:
            return await mock.hosted_session(ref)
        except RecordNotFound:
            return None

    @router.get("/{ref}")
    async def show_mock_payment(ref: SessionRef) -> HTMLResponse:
        hosted = await find(ref)
        return _unknown() if hosted is None else _hosted_page(hosted)

    @router.post("/{ref}/pay")
    async def pay_mock_payment(ref: SessionRef) -> Response:
        if await find(ref) is None:
            return _unknown()
        hosted = await mock.pay(ref)
        if hosted.session.status is not CheckoutStatus.COMPLETE:
            page = _hosted_page(hosted)
            page.status_code = status.HTTP_409_CONFLICT
            return page
        await sender.deliver(mock.completed_event(hosted))
        return RedirectResponse(hosted.success_url, status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/{ref}/cancel")
    async def cancel_mock_payment(ref: SessionRef) -> Response:
        hosted = await find(ref)
        if hosted is None:
            return _unknown()
        return RedirectResponse(hosted.cancel_url, status_code=status.HTTP_303_SEE_OTHER)

    return router
