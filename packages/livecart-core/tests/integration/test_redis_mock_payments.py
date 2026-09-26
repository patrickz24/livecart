"""The mock payment provider on a real Redis: what its hosted page and its webhook rely on.

What every provider does, the fake and the mock alike, is in
``tests/contract/test_payment_provider.py``; this is what only the mock does.
"""

import asyncio
import re
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutRequest,
    CheckoutStatus,
    PaymentProviderName,
)
from livecart_core.application.ports import InvalidSignature, RecordNotFound
from livecart_core.domain.money import Money
from livecart_core.domain.reference import OrderReference
from livecart_core.infrastructure.payments.mock import (
    MOCK_SESSION_TTL_S,
    MockPaymentProvider,
)
from livecart_core.signing import SIGNATURE_HEADER
from livecart_core.testing.fakes import FAKE_CLOCK_START, FakeClock
from livecart_core.testing.keys import TEST_MOCK_PAYMENT_WEBHOOK_SECRET

pytestmark = pytest.mark.integration

BASE_URL = "http://192.168.1.20:8080"
CHECKOUT = CheckoutRequest(
    UUID(int=10),
    1,
    OrderReference("LC-7K3F9Q"),
    Money(2_500, "EUR"),
    f"{BASE_URL}/api/v1/payments/return?status=success&ref=LC-7K3F9Q",
    f"{BASE_URL}/api/v1/payments/return?status=cancel&ref=LC-7K3F9Q",
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _mock(
    client: Redis,
    prefix: str,
    clock: FakeClock,
    *,
    secret: SecretStr = TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
) -> MockPaymentProvider:
    return MockPaymentProvider(
        client, prefix=prefix, secret=secret, clock=clock, public_base_url=f"{BASE_URL}/"
    )


@pytest.fixture
def mock(redis_client: Redis, redis_key_prefix: str, clock: FakeClock) -> MockPaymentProvider:
    return _mock(redis_client, redis_key_prefix, clock)


async def test_a_session_is_named_by_its_attempt_and_hosted_by_the_api(
    mock: MockPaymentProvider, redis_client: Redis, redis_key_prefix: str, clock: FakeClock
) -> None:
    session = await mock.create_checkout(CHECKOUT)
    # Another process, the worker, builds its own mock over the same Redis and key.
    elsewhere = _mock(redis_client, redis_key_prefix, clock)
    other_key = _mock(
        redis_client, redis_key_prefix, clock, secret=SecretStr("another-mock-payment-key-" * 2)
    )

    assert re.fullmatch(r"mock_cs_[0-9a-f]{40}", session.provider_ref)
    assert session.provider_ref == mock.session_ref(CHECKOUT.idempotency_key)
    assert session.redirect_url == f"{BASE_URL}/api/v1/mock-payments/{session.provider_ref}"
    assert await elsewhere.get_checkout(session.provider_ref) == session
    assert other_key.session_ref(CHECKOUT.idempotency_key) != session.provider_ref


async def test_a_session_lasts_a_week_in_redis_under_the_prefix(
    mock: MockPaymentProvider, redis_client: Redis, redis_key_prefix: str
) -> None:
    session = await mock.create_checkout(CHECKOUT)
    await mock.expire_checkout(session.provider_ref)

    keys = sorted([key.decode() async for key in redis_client.scan_iter(f"{redis_key_prefix}:*")])

    assert keys == [
        f"{redis_key_prefix}:mock-payments:outcome:{session.provider_ref}",
        f"{redis_key_prefix}:mock-payments:session:{session.provider_ref}",
    ]
    for key in keys:
        assert MOCK_SESSION_TTL_S - 5 <= await redis_client.ttl(key) <= MOCK_SESSION_TTL_S


async def test_the_hosted_page_reads_what_the_buyer_pays_and_where_it_goes_next(
    mock: MockPaymentProvider,
) -> None:
    session = await mock.create_checkout(CHECKOUT)

    hosted = await mock.hosted_session(session.provider_ref)

    assert hosted.session == session
    assert hosted.reference == CHECKOUT.reference
    assert (hosted.success_url, hosted.cancel_url) == (CHECKOUT.success_url, CHECKOUT.cancel_url)
    assert hosted.completed_at is None
    with pytest.raises(RecordNotFound):
        await mock.hosted_session("mock_cs_404")
    with pytest.raises(RecordNotFound):
        await mock.pay("mock_cs_404")


async def test_paying_gives_one_completed_event_that_the_webhook_verifies(
    mock: MockPaymentProvider, clock: FakeClock
) -> None:
    session = await mock.create_checkout(CHECKOUT)
    clock.advance(timedelta(minutes=2))

    paid = await mock.pay(session.provider_ref)
    clock.advance(timedelta(minutes=1))
    again = await mock.pay(session.provider_ref)
    event = mock.completed_event(paid)
    headers, body = mock.signed_webhook(event)

    assert paid == again
    assert paid.completed_at == FAKE_CLOCK_START + timedelta(minutes=2)
    assert mock.completed_event(again) == event
    assert event.provider is PaymentProviderName.MOCK
    assert event.event_type == CheckoutEventType.COMPLETED.value
    assert re.fullmatch(r"evt_mock_[0-9a-f]{24}", event.event_id)
    assert event.checkout_ref == session.provider_ref
    assert event.payment_ref == paid.session.payment_ref
    assert (event.amount_minor, event.currency) == (2_500, "EUR")
    assert event.occurred_at == paid.completed_at
    assert set(headers) == {SIGNATURE_HEADER}
    assert await mock.verify_webhook(headers, body) == event
    # Signed at the clock's time: stale past the signatures' tolerance.
    clock.advance(timedelta(seconds=301))
    with pytest.raises(InvalidSignature):
        await mock.verify_webhook(headers, body)


async def test_an_open_or_expired_session_has_no_completed_event(
    mock: MockPaymentProvider,
) -> None:
    session = await mock.create_checkout(CHECKOUT)
    expired_session = await mock.create_checkout(replace(CHECKOUT, attempt_no=2))
    await mock.expire_checkout(expired_session.provider_ref)

    still_open = await mock.hosted_session(session.provider_ref)
    paid_too_late = await mock.pay(expired_session.provider_ref)

    assert paid_too_late.session.status is CheckoutStatus.EXPIRED
    for hosted in (still_open, paid_too_late):
        with pytest.raises(ValueError, match="not complete"):
            mock.completed_event(hosted)


async def test_a_payment_racing_the_expiry_leaves_one_outcome(
    mock: MockPaymentProvider,
) -> None:
    sessions = [await mock.create_checkout(replace(CHECKOUT, attempt_no=n)) for n in range(1, 21)]

    async def race(ref: str) -> tuple[CheckoutStatus, bool]:
        paid, expired = await asyncio.gather(mock.pay(ref), mock.expire_checkout(ref))
        return paid.session.status, expired

    results = [await race(session.provider_ref) for session in sessions]

    for (status, expired), session in zip(results, sessions, strict=True):
        final = await mock.get_checkout(session.provider_ref)
        # Expired by the sweep exactly when the payment lost, and never both.
        assert expired is (final.status is CheckoutStatus.EXPIRED)
        assert status is final.status
        assert final.status in (CheckoutStatus.COMPLETE, CheckoutStatus.EXPIRED)
