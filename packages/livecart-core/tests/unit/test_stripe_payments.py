"""The Stripe adapter offline: what it sends Stripe, what it makes of Stripe's answers and webhooks.

Its common behaviour runs in tests/contract/test_payment_provider.py; these
tests pin what only Stripe has: the Checkout Session it asks for, the
signature Stripe's SDK checks, the reduction of Stripe's events, and how
Stripe's errors and retries map onto the port. Every call goes through
``FakeStripeApi`` or a scripted transport: none reaches api.stripe.com.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl
from uuid import UUID

import httpx2
import pytest
import stripe as stripe_sdk

from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    CheckoutRequest,
    CheckoutStatus,
    PaymentEvent,
    PaymentProviderName,
)
from livecart_core.application.ports import InvalidSignature, ServiceUnavailable
from livecart_core.domain.money import Money
from livecart_core.domain.reference import OrderReference
from livecart_core.infrastructure.payments.stripe import (
    Httpx2Client,
    StripePaymentProvider,
    payment_event,
)
from livecart_core.testing.keys import TEST_STRIPE_SECRET_KEY, TEST_STRIPE_WEBHOOK_SECRET
from livecart_core.testing.stripe_api import CUSTOMER_DETAILS, FakeStripeApi

BRIDGE = "http://localhost:8080/api/v1/payments/return"
CHECKOUT = CheckoutRequest(
    UUID("01a0de85-a31d-7a01-bb3a-b7f2f0b6541f"),
    2,
    OrderReference("LC-7K3F9Q"),
    Money(2_500, "EUR"),
    f"{BRIDGE}?status=success&ref=LC-7K3F9Q",
    f"{BRIDGE}?status=cancel&ref=LC-7K3F9Q",
)
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


async def _no_backoff(_seconds: float) -> None:
    return None


def _stripe(transport: httpx2.AsyncBaseTransport, *, retries: int = 2) -> StripePaymentProvider:
    return StripePaymentProvider(
        secret_key=TEST_STRIPE_SECRET_KEY,
        webhook_secret=TEST_STRIPE_WEBHOOK_SECRET,
        http_client=Httpx2Client(transport=transport, sleep=_no_backoff),
        max_network_retries=retries,
    )


def _event(**fields: object) -> PaymentEvent:
    values: dict[str, object] = {
        "provider": PaymentProviderName.STRIPE,
        "event_id": "evt_1",
        "event_type": CheckoutEventType.COMPLETED.value,
        "checkout_ref": "cs_test_1",
        "payment_ref": "pi_test_1",
        "payment_status": CheckoutPaymentStatus.PAID,
        "amount_minor": 2_500,
        "currency": "EUR",
        "occurred_at": datetime.now(UTC).replace(microsecond=0),
    }
    values.update(fields)
    return PaymentEvent.model_validate(values)


# --- The Checkout Session ------------------------------------------------------------------


async def test_a_checkout_asks_stripe_for_a_card_session_of_the_order_total() -> None:
    api = FakeStripeApi()
    stripe = _stripe(api.transport)

    session = await stripe.create_checkout(CHECKOUT)

    [request] = api.requests
    assert (request.method, str(request.url)) == (
        "POST",
        "https://api.stripe.com/v1/checkout/sessions",
    )
    assert request.headers["Authorization"] == f"Bearer {TEST_STRIPE_SECRET_KEY.get_secret_value()}"
    assert request.headers["Idempotency-Key"] == CHECKOUT.idempotency_key
    form = dict(parse_qsl(request.content.decode()))
    assert form == {
        "mode": "payment",
        "payment_method_types[0]": "card",
        "client_reference_id": str(CHECKOUT.order_id),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "eur",
        "line_items[0][price_data][unit_amount]": "2500",
        "line_items[0][price_data][product_data][name]": "LiveCart order LC-7K3F9Q",
        "metadata[order_id]": str(CHECKOUT.order_id),
        "metadata[attempt_no]": "2",
        "metadata[reference]": "LC-7K3F9Q",
        "payment_intent_data[metadata][order_id]": str(CHECKOUT.order_id),
        "payment_intent_data[metadata][attempt_no]": "2",
        "payment_intent_data[metadata][reference]": "LC-7K3F9Q",
        "success_url": CHECKOUT.success_url,
        "cancel_url": CHECKOUT.cancel_url,
    }
    # No expires_at: the sweep expires the session, before it releases the stock.
    assert session.status is CheckoutStatus.OPEN
    assert session.amount == Money(2_500, "EUR")
    assert session.redirect_url == f"https://checkout.stripe.com/c/pay/{session.provider_ref}"


async def test_a_replayed_answer_is_read_back_as_the_session_stands() -> None:
    api = FakeStripeApi()
    stripe = _stripe(api.transport)
    first = await stripe.create_checkout(CHECKOUT)
    api.pay(first.provider_ref)

    again = await stripe.create_checkout(CHECKOUT)

    assert [(r.method, r.url.path) for r in api.requests] == [
        ("POST", "/v1/checkout/sessions"),
        ("POST", "/v1/checkout/sessions"),
        ("GET", f"/v1/checkout/sessions/{first.provider_ref}"),
    ]
    assert again.provider_ref == first.provider_ref
    assert (again.status, again.payment_ref) == (CheckoutStatus.COMPLETE, "pi_test_fake0001")
    # A closed session has no page: its redirect is where Stripe sends the buyer back.
    assert again.redirect_url == CHECKOUT.success_url


async def test_an_expired_session_redirects_to_the_cancel_url() -> None:
    api = FakeStripeApi()
    stripe = _stripe(api.transport)
    session = await stripe.create_checkout(CHECKOUT)
    await stripe.expire_checkout(session.provider_ref)

    expired = await stripe.get_checkout(session.provider_ref)

    assert (expired.status, expired.redirect_url) == (CheckoutStatus.EXPIRED, CHECKOUT.cancel_url)


def _scripted(*answers: httpx2.Response | Exception) -> tuple[httpx2.MockTransport, list[str]]:
    """A transport answering each call in turn, and the paths it was asked."""
    queue = list(answers)
    asked: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        asked.append(f"{request.method} {request.url.path}")
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx2.MockTransport(handle), asked


def _session_json(status: str = "open") -> dict[str, object]:
    return {
        "id": "cs_test_1",
        "object": "checkout.session",
        "status": status,
        "url": "https://checkout.stripe.com/c/pay/cs_test_1" if status == "open" else None,
        "amount_total": 2_500,
        "currency": "eur",
        "payment_intent": None,
        "success_url": CHECKOUT.success_url,
        "cancel_url": CHECKOUT.cancel_url,
    }


def _stripe_error(status: int, error_type: str = "api_error") -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"type": error_type, "message": "scripted"}})


async def test_a_5xx_is_retried_by_the_sdk_under_the_same_key() -> None:
    transport, asked = _scripted(
        _stripe_error(500), _stripe_error(503), httpx2.Response(200, json=_session_json())
    )

    session = await _stripe(transport).create_checkout(CHECKOUT)

    assert session.provider_ref == "cs_test_1"
    assert asked == ["POST /v1/checkout/sessions"] * 3


@pytest.mark.parametrize(
    "failure",
    [
        lambda: _stripe_error(500),
        lambda: _stripe_error(429, "rate_limit_error"),
        lambda: httpx2.ReadTimeout("scripted"),
    ],
    ids=["5xx", "rate-limit", "timeout"],
)
async def test_stripe_out_of_reach_is_unavailable(
    failure: Callable[[], httpx2.Response | Exception],
) -> None:
    transport, asked = _scripted(*(failure() for _ in range(3)))

    with pytest.raises(ServiceUnavailable):
        await _stripe(transport).get_checkout("cs_test_1")
    assert 1 <= len(asked) <= 3


async def test_an_open_session_stripe_refuses_to_expire_is_tried_again_later() -> None:
    refusal = _stripe_error(400, "invalid_request_error")
    transport, asked = _scripted(refusal, httpx2.Response(200, json=_session_json("open")))

    with pytest.raises(ServiceUnavailable, match="refused to expire an open session"):
        await _stripe(transport).expire_checkout("cs_test_1")
    assert asked == [
        "POST /v1/checkout/sessions/cs_test_1/expire",
        "GET /v1/checkout/sessions/cs_test_1",
    ]


async def test_the_http_client_closes_its_connections() -> None:
    stripe = _stripe(FakeStripeApi().transport)

    await stripe.aclose()


# --- Webhooks --------------------------------------------------------------------------------


async def test_a_signed_webhook_is_checked_by_construct_event_and_reduced() -> None:
    api = FakeStripeApi()
    stripe = _stripe(api.transport)
    event = _event()
    headers, body = api.signed(event, TEST_STRIPE_WEBHOOK_SECRET)

    verified = await stripe.verify_webhook(headers, body)

    assert verified == event
    assert CUSTOMER_DETAILS["email"] in body.decode()
    assert "buyer@example.invalid" not in verified.model_dump_json()


async def test_a_signature_older_than_stripes_tolerance_is_refused() -> None:
    api = FakeStripeApi()
    stripe = _stripe(api.transport)
    body = json.dumps(api.event(_event())).encode()
    now = datetime.now(UTC)

    recent = api.sign(body, TEST_STRIPE_WEBHOOK_SECRET, at=now - timedelta(seconds=240))
    stale = api.sign(body, TEST_STRIPE_WEBHOOK_SECRET, at=now - timedelta(seconds=301))

    assert (await stripe.verify_webhook(recent, body)).event_id == "evt_1"
    with pytest.raises(InvalidSignature):
        await stripe.verify_webhook(stale, body)


async def test_a_missing_repeated_or_foreign_signature_is_refused() -> None:
    api = FakeStripeApi()
    stripe = _stripe(api.transport)
    headers, body = api.signed(_event(), TEST_STRIPE_WEBHOOK_SECRET)
    signature = headers["Stripe-Signature"]

    for refused in (
        {},
        {"Stripe-Signature": "t=1,v1=00"},
        {"stripe-signature": signature, "STRIPE-SIGNATURE": signature},
        {"X-LiveCart-Signature": signature},
    ):
        with pytest.raises(InvalidSignature):
            await stripe.verify_webhook(refused, body)
    # Whatever the header's case, once.
    assert (await stripe.verify_webhook({"stripe-signature": signature}, body)).event_id == "evt_1"


async def test_a_verified_body_that_is_no_json_event_is_a_value_error() -> None:
    api = FakeStripeApi()
    stripe = _stripe(api.transport)
    body = b"{not json"

    with pytest.raises(ValueError):  # noqa: PT011 - the JSON decoder words it
        await stripe.verify_webhook(api.sign(body, TEST_STRIPE_WEBHOOK_SECRET), body)


def _stripe_event(data_object: dict[str, object], event_type: str) -> stripe_sdk.Event:
    return stripe_sdk.Event.construct_from(
        {
            "id": "evt_2",
            "object": "event",
            "created": int(NOW.timestamp()),
            "type": event_type,
            "data": {"object": data_object},
        },
        "sk_test_unused",
    )


def test_an_event_of_another_object_keeps_its_id_type_and_time_only() -> None:
    event = _stripe_event(
        {"id": "pi_1", "object": "payment_intent", "amount": 2_500, "currency": "eur"},
        "payment_intent.succeeded",
    )

    reduced = payment_event(event)

    assert reduced == PaymentEvent(
        provider=PaymentProviderName.STRIPE,
        event_id="evt_2",
        event_type="payment_intent.succeeded",
        occurred_at=NOW,
    )


def test_a_session_of_an_unknown_payment_status_or_without_amount_keeps_the_rest() -> None:
    event = _stripe_event(
        {
            "id": "cs_test_1",
            "object": "checkout.session",
            "payment_intent": {"id": "pi_test_1", "object": "payment_intent"},
            "payment_status": "a_status_of_tomorrow",
            "amount_total": None,
            "currency": "eur",
        },
        CheckoutEventType.EXPIRED.value,
    )

    reduced = payment_event(event)

    assert (reduced.checkout_ref, reduced.payment_ref) == ("cs_test_1", "pi_test_1")
    assert (reduced.payment_status, reduced.amount) == (None, None)
