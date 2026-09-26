"""``PaymentProvider`` on Stripe Checkout, in test mode: ``PAYMENT_PROVIDER=stripe``.

A hosted Checkout Session per payment attempt, by stripe-python's
``StripeClient`` over its async API:

- ``create_checkout`` creates the session (``mode=payment``, cards only, one
  line of the order's total, no ``expires_at``: the sweep expires it) under
  the attempt's idempotency key, ``checkout-session:{order_id}:{attempt_no}``.
  The same key sent again gets the same session: Stripe replays its first
  answer (``Idempotent-Replayed: true``) for 24 hours, and the session is
  then read back, to report it as it stands; the same key with other
  parameters is Stripe's ``IdempotencyError``, a ``ValueError`` here. The
  session carries the order's id, attempt and reference as metadata, never
  the buyer's token;
- ``expire_checkout`` expires an open session; Stripe refuses to expire a
  session that is no longer open (paid, or expired already), which the
  adapter reads back to answer ``False``. That refusal is what settles the
  race between a late payment and the release of the stock (expire-first);
- ``get_checkout`` reads a session back. A session that left ``open`` has no
  hosted page any more (Stripe answers its ``url`` as null): its redirect
  URL is then where Stripe would have sent the buyer back, the bridge page's
  success or cancel URL;
- ``verify_webhook`` checks the ``Stripe-Signature`` header with
  ``StripeClient.construct_event``, under ``STRIPE_WEBHOOK_SECRET`` and
  Stripe's default tolerance of 300 s, before it parses the body, then
  reduces the event to ``PaymentEvent``: its id, type and time, and of a
  checkout session only its id, payment intent, payment status, amount and
  currency. The customer's details, and everything else, stop here.

The HTTP calls go through ``Httpx2Client``, stripe-python's ``HTTPClient``
over httpx2, the HTTP client of the rest of the repository, whose transport
the tests replace: no test ever reaches api.stripe.com. A transport failure,
a rate limit or a 5xx answer, after the SDK's own retries, raises
``ServiceUnavailable``; a session Stripe does not know, ``RecordNotFound``.
The secret key must be a test one (``settings``): LiveCart takes no live
payments.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Final, cast

import httpx2
import stripe
from pydantic import SecretStr
from stripe.params.checkout import SessionCreateParams

from livecart_core.application.dto import (
    CheckoutPaymentStatus,
    CheckoutRequest,
    CheckoutSession,
    CheckoutStatus,
    PaymentEvent,
    PaymentProviderName,
)
from livecart_core.application.ports import InvalidSignature, RecordNotFound, ServiceUnavailable
from livecart_core.domain.money import Money
from livecart_core.signing import header_value

SIGNATURE_HEADER: Final = "Stripe-Signature"
# The retries the SDK makes of a call that failed on the network, a 409 or a
# 5xx, with its own backoff (0.5 s, then 1 s, jittered): three tries in all.
MAX_NETWORK_RETRIES: Final = 2
# The longest one try of a call may take.
STRIPE_TIMEOUT_S: Final = 10.0
# What Stripe answers for an id it does not know.
RESOURCE_MISSING: Final = "resource_missing"
# The header of an answer Stripe replayed for an idempotency key it saw before.
IDEMPOTENT_REPLAYED: Final = "idempotent-replayed"
# The SDK ships the error's constructor without annotations: the cast gives it its type.
_connection_error: Final = cast(Callable[..., stripe.APIConnectionError], stripe.APIConnectionError)


class Httpx2Client(stripe.HTTPClient):
    """stripe-python's HTTP client over httpx2, asynchronous only.

    ``transport`` replaces the network in the tests, ``sleep`` the SDK's
    backoff between its retries.
    """

    name = "httpx2"

    def __init__(
        self,
        *,
        timeout_s: float = STRIPE_TIMEOUT_S,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__()
        self._client = httpx2.AsyncClient(timeout=timeout_s, transport=transport)
        self._sleep = sleep

    async def request_async(
        self, method: str, url: str, headers: Mapping[str, str], post_data: Any = None
    ) -> tuple[bytes, int, Mapping[str, str]]:
        try:
            response = await self._client.request(
                method, url, headers=dict(headers), content=post_data
            )
        except httpx2.HTTPError as error:
            # The SDK retries a connection error, then raises it.
            raise _connection_error(
                f"network error talking to Stripe: {type(error).__name__}", should_retry=True
            ) from error
        return response.content, response.status_code, response.headers

    def sleep_async(self, secs: float) -> Awaitable[None]:
        return self._sleep(secs)

    async def close_async(self) -> None:
        await self._client.aclose()

    def close(self) -> None:
        """Nothing to close synchronously: ``close_async`` releases the connections."""


@asynccontextmanager
async def _stripe_errors() -> AsyncIterator[None]:
    """Stripe's errors as the port's: transient ones ``ServiceUnavailable``, unknown ids 404."""
    try:
        yield
    except stripe.IdempotencyError as error:
        raise ValueError("idempotency key reused with other parameters") from error
    except stripe.InvalidRequestError as error:
        if error.code == RESOURCE_MISSING:
            raise RecordNotFound("no such Stripe checkout session") from error
        raise
    except (stripe.APIConnectionError, stripe.RateLimitError) as error:
        raise ServiceUnavailable(f"Stripe unavailable: {type(error).__name__}") from error
    except stripe.APIError as error:
        raise ServiceUnavailable(f"Stripe unavailable: HTTP {error.http_status}") from error


def _checkout_session(session: stripe.checkout.Session) -> CheckoutSession:
    if session.status is None:
        raise ValueError(f"Stripe answered session {session.id} without its status")
    status = CheckoutStatus(session.status)
    redirect_url = session.url
    if redirect_url is None:
        # A closed session has no page: where Stripe sends its buyer back.
        complete = status is CheckoutStatus.COMPLETE
        redirect_url = session.success_url if complete else session.cancel_url
    amount, currency = session.amount_total, session.currency
    if redirect_url is None or amount is None or currency is None:
        raise ValueError(f"Stripe answered session {session.id} without its URLs or its amount")
    return CheckoutSession(
        provider_ref=session.id,
        redirect_url=redirect_url,
        status=status,
        amount=Money(amount, currency.upper()),
        payment_ref=_id_of(session.payment_intent),
    )


def _id_of(value: object) -> str | None:
    """The id of an object Stripe gives by id, or expanded."""
    if value is None or isinstance(value, str):
        return value
    identifier = value.get("id") if isinstance(value, dict) else getattr(value, "id", None)
    return identifier if isinstance(identifier, str) else None


def _payment_status(value: object) -> CheckoutPaymentStatus | None:
    """Stripe's ``payment_status``, or ``None`` for one this model does not know."""
    try:
        return CheckoutPaymentStatus(value) if isinstance(value, str) else None
    except ValueError:
        return None


def payment_event(event: stripe.Event) -> PaymentEvent:
    """A verified Stripe event reduced to the allow-list: see the module docstring."""
    session = event.data.object.to_dict()
    fields: dict[str, object] = {}
    if session.get("object") == "checkout.session":
        amount, currency = session.get("amount_total"), session.get("currency")
        fields = {
            "checkout_ref": session.get("id"),
            "payment_ref": _id_of(session.get("payment_intent")),
            "payment_status": _payment_status(session.get("payment_status")),
        }
        if amount is not None and currency is not None:
            fields |= {"amount_minor": amount, "currency": str(currency).upper()}
    return PaymentEvent.model_validate(
        {
            "provider": PaymentProviderName.STRIPE,
            "event_id": event.id,
            "event_type": event.type,
            "occurred_at": datetime.fromtimestamp(event.created, UTC),
            **fields,
        }
    )


class StripePaymentProvider:
    """Stripe Checkout, in test mode; see the module docstring."""

    def __init__(
        self,
        *,
        secret_key: SecretStr,
        webhook_secret: SecretStr,
        http_client: Httpx2Client | None = None,
        max_network_retries: int = MAX_NETWORK_RETRIES,
    ) -> None:
        self._http_client = http_client if http_client is not None else Httpx2Client()
        self._client = stripe.StripeClient(
            secret_key.get_secret_value(),
            http_client=self._http_client,
            max_network_retries=max_network_retries,
        )
        self._webhook_secret = webhook_secret

    def __repr__(self) -> str:
        return "StripePaymentProvider(secret_key=SecretStr('**********'))"

    @property
    def name(self) -> PaymentProviderName:
        return PaymentProviderName.STRIPE

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        metadata = {
            "order_id": str(request.order_id),
            "attempt_no": str(request.attempt_no),
            "reference": request.reference.value,
        }
        params: SessionCreateParams = {
            "mode": "payment",
            "payment_method_types": ["card"],
            "client_reference_id": str(request.order_id),
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": request.amount.currency.lower(),
                        "unit_amount": request.amount.amount_minor,
                        "product_data": {"name": f"LiveCart order {request.reference.value}"},
                    },
                }
            ],
            "metadata": metadata,
            "payment_intent_data": {"metadata": metadata},
            "success_url": request.success_url,
            "cancel_url": request.cancel_url,
        }
        sessions = self._client.v1.checkout.sessions
        async with _stripe_errors():
            session = await sessions.create_async(
                params, {"idempotency_key": request.idempotency_key}
            )
            response = session.last_response
            if response is not None and response.headers.get(IDEMPOTENT_REPLAYED) == "true":
                # Stripe replayed the answer of the first call: read the session as it stands.
                session = await sessions.retrieve_async(session.id)
        return _checkout_session(session)

    async def expire_checkout(self, provider_ref: str) -> bool:
        sessions = self._client.v1.checkout.sessions
        async with _stripe_errors():
            try:
                await sessions.expire_async(provider_ref)
            except stripe.InvalidRequestError as error:
                if error.code == RESOURCE_MISSING:
                    raise
                # Stripe expires open sessions only: read it back to tell.
                session = await sessions.retrieve_async(provider_ref)
                if session.status == CheckoutStatus.OPEN.value:
                    raise ServiceUnavailable("Stripe refused to expire an open session") from error
                return False
        return True

    async def get_checkout(self, provider_ref: str) -> CheckoutSession:
        async with _stripe_errors():
            session = await self._client.v1.checkout.sessions.retrieve_async(provider_ref)
        return _checkout_session(session)

    async def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> PaymentEvent:
        signature = header_value(headers, SIGNATURE_HEADER)
        try:
            event = self._client.construct_event(
                raw_body, signature, self._webhook_secret.get_secret_value()
            )
        except stripe.SignatureVerificationError as error:
            raise InvalidSignature("the Stripe signature does not hold") from error
        # A body that verifies but is no event: construct_event's own ValueError.
        return payment_event(event)

    async def aclose(self) -> None:
        """Release the HTTP client's connections."""
        await self._http_client.close_async()
