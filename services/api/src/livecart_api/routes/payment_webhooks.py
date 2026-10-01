"""The payment webhook: ``POST /api/v1/webhooks/payments/{provider}``.

The payment provider (Stripe, or the mock's hosted page standing for it)
posts each event of a checkout session here, and delivers it again until it
gets a 2xx. The API only makes the event durable; the worker applies it
(``livecart_worker.payments``). In this order, each step before the next one
reads anything more:

1. only the provider ``PAYMENT_PROVIDER`` selects has a webhook: another
   provider in the path answers 404;
2. an address with 60 failed signatures in the minute gets 429 with
   ``Retry-After`` before its body is read, as on the comment webhook;
3. the raw body is read once, capped at 1 MiB (413), and the provider's
   adapter checks its signature before anything parses it:
   ``stripe.Webhook.construct_event`` under ``STRIPE_WEBHOOK_SECRET`` with
   Stripe's default tolerance of 300 s, or the HMAC of
   ``livecart_core.signing`` under ``MOCK_PAYMENT_WEBHOOK_SECRET``. A missing
   or wrong signature is a 401 whose body never says which check failed,
   counted against the address, and so is a Stripe body that is no UTF-8,
   which Stripe never signs; a verified body that is no event of the
   provider is a 422;
4. the event, reduced by the adapter to its allow-list (``PaymentEvent``,
   without the customer's details), goes into ``processed_webhook_events``:
   ``INSERT ... ON CONFLICT (provider, event_id) DO UPDATE SET
   delivery_count = delivery_count + 1``, then 200. A redelivery is counted
   and answered 200 as well, since the event is already durable.

Nothing on this path needs Redis: the limit of step 2 fails open when Redis
is down, and a provider's event is recorded all the same. PostgreSQL down is
a 503 with ``Retry-After``, and the provider delivers the event again later.
"""

from typing import Final

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from livecart_api.errors import NOT_FOUND, documented
from livecart_api.rate_limits import TOO_MANY_REQUESTS, RateLimiting, client_address
from livecart_core.application.dto import PaymentProviderName
from livecart_core.application.payment_webhooks import ReceivePaymentWebhook
from livecart_core.application.ports import InvalidSignature

logger = structlog.get_logger(__name__)

PREFIX: Final = "/api/v1/webhooks/payments"
INVALID_SIGNATURE: Final = "Invalid signature"
NO_EVENT: Final = "The body is no payment event of this provider"
REQUEST_BODY: Final = {
    "required": True,
    "description": (
        "The provider's event, as the provider sends it. Stripe signs it with its "
        "Stripe-Signature header (STRIPE_WEBHOOK_SECRET); the mock provider sends a "
        "PaymentEvent, signed with X-LiveCart-Signature: t=<unix time>,v1=<hex "
        "HMAC-SHA256 of '<t>.<body>' under MOCK_PAYMENT_WEBHOOK_SECRET>."
    ),
    "content": {"application/json": {"schema": {"type": "object"}}},
}


class PaymentWebhookReceived(BaseModel):
    """A verified payment event, durable in the registry: the worker applies it."""

    redelivery: bool = Field(
        description=(
            "Whether an earlier delivery recorded this event already: the registry counted "
            "this one, and the event is applied once all the same."
        )
    )


def build_payment_webhook_router(receive: ReceivePaymentWebhook, limits: RateLimiting) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["webhooks"])

    @router.post(
        "/{provider}",
        summary="Receive an event of a checkout session from the payment provider",
        openapi_extra={"requestBody": REQUEST_BODY},
        responses=documented(
            {
                status.HTTP_401_UNAUTHORIZED: "The signature is missing, wrong or too old",
                status.HTTP_404_NOT_FOUND: "The provider is not the one this API takes",
                status.HTTP_429_TOO_MANY_REQUESTS: TOO_MANY_REQUESTS,
                status.HTTP_503_SERVICE_UNAVAILABLE: (
                    "The registry cannot record the event now: deliver it again later"
                ),
            }
        ),
    )
    async def receive_payment_webhook(
        provider: PaymentProviderName, request: Request
    ) -> PaymentWebhookReceived:
        if provider is not receive.provider:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        address = client_address(request)
        failures = limits.rules.payment_webhook_signature_failures
        await limits.check(failures, address)
        raw_body = await request.body()
        try:
            received = await receive(request.headers, raw_body)
        except InvalidSignature:
            logger.warning("payment_webhook_signature_refused", provider=provider.value)
            await limits.record(failures, address)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=INVALID_SIGNATURE) from None
        except ValueError:
            # A verified body the adapter cannot read: its message may quote the body.
            logger.warning("payment_webhook_unreadable", provider=provider.value)
            refusal = {"loc": ("body",), "msg": NO_EVENT, "type": "value_error"}
            raise RequestValidationError([refusal]) from None
        event = received.event
        logger.info(
            "payment_webhook_recorded",
            provider=provider.value,
            event_id=event.event_id,
            event_type=event.event_type,
            redelivery=received.redelivery,
        )
        return PaymentWebhookReceived(redelivery=received.redelivery)

    return router
