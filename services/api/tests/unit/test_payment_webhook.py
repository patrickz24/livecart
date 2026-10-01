"""The payment webhook over the fakes: the provider's signature first, then the registry, then 200.

The provider is the mock, whose signature is the HMAC of ``livecart_core.signing``
under ``MOCK_PAYMENT_WEBHOOK_SECRET``: it checks signatures without its Redis.
The last tests run Stripe's adapter, whose SDK checks ``Stripe-Signature``
(``construct_event``), over a fake Stripe.
"""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from pydantic import SecretStr

from livecart_api.app import MAX_REQUEST_BODY_BYTES, create_app
from livecart_api.rate_limits import TOO_MANY_REQUESTS
from livecart_api.routes.payment_webhooks import INVALID_SIGNATURE, NO_EVENT
from livecart_api.testing import current_time_clock, make_settings, memory_resources, open_client
from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    PaymentEvent,
    PaymentProviderName,
    RateLimitKey,
    RegisteredEvent,
    WindowCount,
)
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.infrastructure.payments.mock import MockPaymentProvider
from livecart_core.infrastructure.payments.stripe import Httpx2Client, StripePaymentProvider
from livecart_core.infrastructure.redis_client import create_redis_client
from livecart_core.signing import SIGNATURE_HEADER, WebhookSigner
from livecart_core.testing.fakes import (
    FakeClock,
    FakePaymentProvider,
    InMemoryDatabase,
    InMemoryRateLimiter,
)
from livecart_core.testing.keys import (
    TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
    TEST_STRIPE_SECRET_KEY,
    TEST_STRIPE_WEBHOOK_SECRET,
)
from livecart_core.testing.stripe_api import FakeStripeApi

MOCK_WEBHOOK = "/api/v1/webhooks/payments/mock"
REFUSED = {"detail": INVALID_SIGNATURE}
# A Redis no unit test reaches: the mock checks signatures without it.
NOWHERE = SecretStr("redis://:unit@redis.invalid:6379/0")


def completed(clock: FakeClock, event_id: str = "evt_mock_1") -> PaymentEvent:
    return PaymentEvent(
        provider=PaymentProviderName.MOCK,
        event_id=event_id,
        event_type=CheckoutEventType.COMPLETED.value,
        checkout_ref="mock_cs_1",
        payment_ref="mock_pi_1",
        payment_status=CheckoutPaymentStatus.PAID,
        amount_minor=2_500,
        currency="EUR",
        occurred_at=clock.now() - timedelta(seconds=2),
    )


@dataclass
class Api:
    client: httpx2.AsyncClient
    clock: FakeClock
    database: InMemoryDatabase
    limiter: InMemoryRateLimiter
    mock: MockPaymentProvider

    def sign(self, body: bytes, *, age_seconds: int = 0) -> dict[str, str]:
        """The mock's signature of ``body``, made ``age_seconds`` ago."""
        timestamp = int(self.clock.now().timestamp()) - age_seconds
        signer = WebhookSigner(TEST_MOCK_PAYMENT_WEBHOOK_SECRET)
        return {SIGNATURE_HEADER: signer.header(body, timestamp=timestamp)}

    async def post(self, event: PaymentEvent) -> httpx2.Response:
        body = event.model_dump_json().encode()
        return await self.client.post(MOCK_WEBHOOK, content=body, headers=self.sign(body))

    def registered(self, event: PaymentEvent) -> RegisteredEvent | None:
        return self.database.tables.webhook_events.get((event.provider, event.event_id))


@asynccontextmanager
async def _open_api() -> AsyncIterator[Api]:
    clock = current_time_clock()
    database, limiter = InMemoryDatabase(), InMemoryRateLimiter(clock)
    redis = create_redis_client(NOWHERE)
    mock = MockPaymentProvider(
        redis,
        prefix="unit",
        secret=TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
        clock=clock,
        public_base_url="http://localhost:8080",
    )
    resources = memory_resources(database, clock=clock, rate_limiter=limiter, payment_provider=mock)
    app = create_app(make_settings(), resources)
    try:
        async with open_client(app) as client:
            yield Api(client, clock, database, limiter, mock)
    finally:
        await redis.aclose()


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with _open_api() as opened:
        yield opened


# --- Recorded --------------------------------------------------------------------------


async def test_a_signed_event_is_recorded_and_acknowledged_with_200(api: Api) -> None:
    event = completed(api.clock)

    response = await api.post(event)

    assert (response.status_code, response.json()) == (200, {"redelivery": False})
    registered = api.registered(event)
    assert registered is not None
    assert registered.event == event
    assert (registered.received_at, registered.delivery_count) == (api.clock.now(), 1)
    assert (registered.attempts, registered.processed_at) == (0, None)


async def test_the_mock_signs_the_webhook_its_own_way(api: Api) -> None:
    event = completed(api.clock)
    headers, body = api.mock.signed_webhook(event)

    response = await api.client.post(MOCK_WEBHOOK, content=body, headers=headers)

    assert response.status_code == 200
    assert api.registered(event) is not None


async def test_a_redelivered_event_is_counted_once_recorded(api: Api) -> None:
    event = completed(api.clock)

    answers = [await api.post(event) for _ in range(5)]

    assert [answer.status_code for answer in answers] == [200] * 5
    assert [answer.json()["redelivery"] for answer in answers] == [False, True, True, True, True]
    registered = api.registered(event)
    assert registered is not None
    assert (registered.event, registered.delivery_count) == (event, 5)
    assert len(api.database.tables.webhook_events) == 1


# --- The signature, before anything else -----------------------------------------------


async def test_an_unsigned_forged_or_stale_event_gets_one_generic_401(api: Api) -> None:
    body = completed(api.clock).model_dump_json().encode()
    signature = api.sign(body)[SIGNATURE_HEADER]
    forged = {SIGNATURE_HEADER: signature[:-1] + ("1" if signature[-1] == "0" else "0")}

    answers = [
        await api.client.post(MOCK_WEBHOOK, content=body),
        await api.client.post(MOCK_WEBHOOK, content=body, headers=forged),
        await api.client.post(MOCK_WEBHOOK, content=body, headers=api.sign(body, age_seconds=301)),
        await api.client.post(MOCK_WEBHOOK, content=body + b" ", headers=api.sign(body)),
        await api.client.post(MOCK_WEBHOOK, content=b"{not json"),
    ]

    assert [answer.status_code for answer in answers] == [401] * 5
    assert all(answer.json() == REFUSED for answer in answers)
    assert api.database.tables.webhook_events == {}
    # Each refusal counts against the address, and against nothing else.
    assert {key.rule: hits for key, (hits, _) in api.limiter.windows.items()} == {
        "payment_webhook_signature_failures": 5
    }


async def test_a_signature_up_to_300_s_old_is_accepted(api: Api) -> None:
    body = completed(api.clock).model_dump_json().encode()

    response = await api.client.post(
        MOCK_WEBHOOK, content=body, headers=api.sign(body, age_seconds=300)
    )

    assert response.status_code == 200


async def test_a_verified_body_that_is_no_event_of_the_provider_answers_422(api: Api) -> None:
    stripe = completed(api.clock).model_copy(update={"provider": PaymentProviderName.STRIPE})
    other_body = stripe.model_dump_json().encode()
    customer = completed(api.clock).model_dump(mode="json")
    customer["customer_details"] = {"email": "buyer@example.invalid"}
    customer_body = json.dumps(customer).encode()

    answers = [
        await api.client.post(MOCK_WEBHOOK, content=other_body, headers=api.sign(other_body)),
        await api.client.post(MOCK_WEBHOOK, content=b"[]", headers=api.sign(b"[]")),
        await api.client.post(MOCK_WEBHOOK, content=customer_body, headers=api.sign(customer_body)),
    ]

    for answer in answers:
        assert answer.status_code == 422
        assert [error["msg"] for error in answer.json()["detail"]] == [NO_EVENT]
        assert "buyer@example.invalid" not in answer.text
    assert api.database.tables.webhook_events == {}
    # A verified body is no forgery: nothing counts against the address.
    assert api.limiter.windows == {}


async def test_only_the_configured_provider_has_a_webhook(api: Api) -> None:
    event = completed(api.clock)
    body = event.model_dump_json().encode()

    stripe = await api.client.post(
        "/api/v1/webhooks/payments/stripe", content=body, headers=api.sign(body)
    )
    unknown = await api.client.post(
        "/api/v1/webhooks/payments/bank", content=body, headers=api.sign(body)
    )

    assert (stripe.status_code, stripe.json()) == (404, {"detail": "Not found"})
    assert unknown.status_code == 422
    assert api.database.tables.webhook_events == {}


async def test_a_body_over_1_mib_is_refused_before_its_signature_is_checked(api: Api) -> None:
    response = await api.client.post(MOCK_WEBHOOK, content=b"x" * (MAX_REQUEST_BODY_BYTES + 1))

    assert response.status_code == 413
    assert api.database.tables.webhook_events == {}


# --- Limits and failures ---------------------------------------------------------------


class CountingProvider(FakePaymentProvider):
    """A fake mock provider that counts the webhooks it verifies, each a whole body read."""

    def __init__(self) -> None:
        super().__init__(PaymentProviderName.MOCK)
        self.verified = 0

    async def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> PaymentEvent:
        self.verified += 1
        return await super().verify_webhook(headers, raw_body)


async def test_an_address_past_its_signature_failures_gets_429_before_its_body_is_read() -> None:
    clock = current_time_clock()
    database, limiter = InMemoryDatabase(), InMemoryRateLimiter(clock)
    provider = CountingProvider()
    resources = memory_resources(
        database, clock=clock, rate_limiter=limiter, payment_provider=provider
    )
    settings = make_settings(rate_limit_payment_webhook_signature_failures_per_minute=2)
    app = create_app(settings, resources)
    headers, body = provider.signed_webhook(completed(clock))

    async with open_client(app, peer="203.0.113.7") as client:
        refused = [(await client.post(MOCK_WEBHOOK, content=body)).status_code for _ in range(2)]
        limited = await client.post(MOCK_WEBHOOK, content=body, headers=headers)
    async with open_client(app, peer="198.51.100.9") as other:
        elsewhere = await other.post(MOCK_WEBHOOK, content=body, headers=headers)

    assert refused == [401, 401]
    assert (limited.status_code, limited.json()) == (429, {"detail": TOO_MANY_REQUESTS})
    assert limited.headers["Retry-After"] == "60"
    # Refused before its signature was checked: two failures and one call elsewhere.
    assert provider.verified == 3
    assert elsewhere.status_code == 200
    assert len(database.tables.webhook_events) == 1


class DownLimiter:
    """Counters that cannot be reached, as when Redis is down."""

    async def hit(self, key: RateLimitKey, *, window_seconds: int) -> WindowCount:
        raise ServiceUnavailable("redis down")

    async def peek(self, key: RateLimitKey) -> WindowCount:
        raise ServiceUnavailable("redis down")


async def test_a_signed_event_is_recorded_while_the_counters_are_down() -> None:
    clock = current_time_clock()
    database = InMemoryDatabase()
    provider = FakePaymentProvider(PaymentProviderName.MOCK)
    resources = memory_resources(
        database, clock=clock, rate_limiter=DownLimiter(), payment_provider=provider
    )
    app = create_app(make_settings(), resources)
    headers, body = provider.signed_webhook(completed(clock))

    async with open_client(app) as client:
        recorded = await client.post(MOCK_WEBHOOK, content=body, headers=headers)
        refused = await client.post(MOCK_WEBHOOK, content=body)

    assert recorded.status_code == 200
    assert refused.status_code == 401
    assert len(database.tables.webhook_events) == 1


class DownRegistry:
    """A registry whose database cannot be reached."""

    async def record(self, event: PaymentEvent, *, now: object) -> bool:
        raise ServiceUnavailable("database down")


async def test_a_registry_that_cannot_record_answers_503_for_the_provider_to_retry() -> None:
    clock = current_time_clock()
    provider = FakePaymentProvider(PaymentProviderName.MOCK)
    resources = memory_resources(
        InMemoryDatabase(), clock=clock, payment_provider=provider, webhook_events=DownRegistry()
    )
    app = create_app(make_settings(), resources)
    headers, body = provider.signed_webhook(completed(clock))

    async with open_client(app) as client:
        response = await client.post(MOCK_WEBHOOK, content=body, headers=headers)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"


async def test_the_webhook_is_documented_with_its_signed_body() -> None:
    async with _open_api() as api:
        schema = (await api.client.get("/openapi.json")).json()

    operation = schema["paths"]["/api/v1/webhooks/payments/{provider}"]["post"]
    assert "Stripe-Signature" in operation["requestBody"]["description"]
    assert set(operation["responses"]) == {"200", "401", "404", "422", "429", "503"}


# --- Stripe ------------------------------------------------------------------------------

STRIPE_WEBHOOK = "/api/v1/webhooks/payments/stripe"


async def _no_backoff(_seconds: float) -> None:
    return None


@asynccontextmanager
async def _open_stripe_api() -> AsyncIterator[tuple[httpx2.AsyncClient, InMemoryDatabase]]:
    """The app with PAYMENT_PROVIDER=stripe: the adapter and its SDK over a fake Stripe."""
    clock = current_time_clock()
    database = InMemoryDatabase()
    stripe = StripePaymentProvider(
        secret_key=TEST_STRIPE_SECRET_KEY,
        webhook_secret=TEST_STRIPE_WEBHOOK_SECRET,
        http_client=Httpx2Client(transport=FakeStripeApi().transport, sleep=_no_backoff),
    )
    resources = memory_resources(database, clock=clock, payment_provider=stripe)
    settings = make_settings(payment_provider=PaymentProviderName.STRIPE)
    try:
        async with open_client(create_app(settings, resources)) as client:
            yield client, database
    finally:
        await stripe.aclose()


def _stripe_event(**fields: object) -> PaymentEvent:
    values: dict[str, object] = {
        "provider": PaymentProviderName.STRIPE,
        "event_id": "evt_test_1",
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


async def test_a_stripe_event_is_verified_by_construct_event_then_recorded() -> None:
    event = _stripe_event()
    headers, body = FakeStripeApi().signed(event, TEST_STRIPE_WEBHOOK_SECRET)

    async with _open_stripe_api() as (client, database):
        first = await client.post(STRIPE_WEBHOOK, content=body, headers=headers)
        again = await client.post(STRIPE_WEBHOOK, content=body, headers=headers)
        mock = await client.post(MOCK_WEBHOOK, content=body, headers=headers)

    assert (first.status_code, first.json()) == (200, {"redelivery": False})
    assert (again.status_code, again.json()) == (200, {"redelivery": True})
    assert mock.status_code == 404
    registered = database.tables.webhook_events[(PaymentProviderName.STRIPE, "evt_test_1")]
    assert (registered.event, registered.delivery_count) == (event, 2)
    # Stripe's body carries the customer's details; the registry never does.
    assert b"buyer@example.invalid" in body
    assert "buyer@example.invalid" not in registered.event.model_dump_json()


async def test_a_stripe_event_unsigned_forged_or_stale_gets_401() -> None:
    api = FakeStripeApi()
    event = _stripe_event()
    _, body = api.signed(event, TEST_STRIPE_WEBHOOK_SECRET)
    forged, _ = api.signed(event, SecretStr("whsec_a-secret-the-forger-guessed"))
    stale = api.sign(
        body, TEST_STRIPE_WEBHOOK_SECRET, at=datetime.now(UTC) - timedelta(seconds=301)
    )

    async with _open_stripe_api() as (client, database):
        answers = [
            await client.post(STRIPE_WEBHOOK, content=body, headers=headers)
            for headers in ({}, forged, stale)
        ]

    assert [(answer.status_code, answer.json()) for answer in answers] == [(401, REFUSED)] * 3
    assert database.tables.webhook_events == {}


async def test_a_stripe_body_that_is_no_utf_8_is_a_refusal_counted_against_the_address() -> None:
    # Stripe's SDK decodes the body before it checks the header: its decoding
    # error must not pass for an unreadable event (422), which counts nothing.
    headers, body = FakeStripeApi().signed(_stripe_event(), TEST_STRIPE_WEBHOOK_SECRET)
    binary = b"\xff\xfe" + body

    async with _open_stripe_api() as (client, database):
        refused = [
            await client.post(STRIPE_WEBHOOK, content=binary, headers=headers) for _ in range(60)
        ]
        limited = await client.post(STRIPE_WEBHOOK, content=binary, headers=headers)

    assert [(answer.status_code, answer.json()) for answer in refused] == [(401, REFUSED)] * 60
    # The default limit, 60 refusals a minute per address: the 61st call is refused unread.
    assert (limited.status_code, limited.json()) == (429, {"detail": TOO_MANY_REQUESTS})
    assert database.tables.webhook_events == {}
