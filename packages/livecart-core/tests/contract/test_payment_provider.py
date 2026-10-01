"""``PaymentProvider`` on every backend: the in-memory fake, the mock on Redis, then Stripe.

Stripe's run is its real adapter and SDK over ``FakeStripeApi``, an
in-memory stand-in of its API behind an httpx2 transport.

A hosted session per payment attempt, the same one for every call with the
attempt's idempotency key; a session that leaves ``open`` once, paid by the
buyer or expired by LiveCart, whichever comes first (expire-first); and
webhooks that verify only under the provider's key.
"""

from dataclasses import replace
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    CheckoutRequest,
    CheckoutStatus,
    PaymentEvent,
    PaymentProviderName,
)
from livecart_core.application.ports import (
    InvalidSignature,
    PaymentProvider,
    RecordNotFound,
    ServiceUnavailable,
)
from livecart_core.domain.money import Money
from livecart_core.domain.reference import OrderReference
from livecart_core.testing.backends import PaymentProviderBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

CHECKOUT = CheckoutRequest(
    UUID(int=10),
    1,
    OrderReference("LC-7K3F9Q"),
    Money(2_500, "EUR"),
    "http://localhost:8080/api/v1/payments/return?status=success&ref=LC-7K3F9Q",
    "http://localhost:8080/api/v1/payments/return?status=cancel&ref=LC-7K3F9Q",
)
EVENT = PaymentEvent(
    provider=PaymentProviderName.MOCK,
    event_id="evt_1",
    event_type=CheckoutEventType.COMPLETED.value,
    checkout_ref="cs_1",
    payment_ref="pi_1",
    amount_minor=2_500,
    currency="EUR",
    occurred_at=FAKE_CLOCK_START,
)


async def test_a_checkout_opens_a_session_for_the_amount(
    payment_provider: PaymentProviderBackend,
) -> None:
    provider = payment_provider.provider

    session = await provider.create_checkout(CHECKOUT)

    assert session.status is CheckoutStatus.OPEN
    assert session.amount == Money(2_500, "EUR")
    assert session.payment_ref is None
    assert session.redirect_url.endswith(session.provider_ref)
    assert await provider.get_checkout(session.provider_ref) == session


async def test_the_same_attempt_always_gets_the_same_session(
    payment_provider: PaymentProviderBackend,
) -> None:
    provider = payment_provider.provider

    first = await provider.create_checkout(CHECKOUT)
    again = await provider.create_checkout(CHECKOUT)
    second_attempt = await provider.create_checkout(replace(CHECKOUT, attempt_no=2))
    other_order = await provider.create_checkout(replace(CHECKOUT, order_id=UUID(int=11)))

    assert again == first
    assert second_attempt.provider_ref != first.provider_ref
    assert other_order.provider_ref not in (first.provider_ref, second_attempt.provider_ref)


async def test_a_reused_key_with_other_parameters_is_refused(
    payment_provider: PaymentProviderBackend,
) -> None:
    provider = payment_provider.provider
    session = await provider.create_checkout(CHECKOUT)

    with pytest.raises(ValueError, match="reused with other parameters"):
        await provider.create_checkout(replace(CHECKOUT, amount=Money(1, "EUR")))
    with pytest.raises(ValueError, match="reused with other parameters"):
        await provider.create_checkout(replace(CHECKOUT, success_url="https://example.invalid/"))
    assert (await provider.get_checkout(session.provider_ref)).amount == Money(2_500, "EUR")


async def test_only_an_open_session_can_be_expired(
    payment_provider: PaymentProviderBackend,
) -> None:
    provider = payment_provider.provider
    session = await provider.create_checkout(CHECKOUT)

    assert await provider.expire_checkout(session.provider_ref) is True
    assert (await provider.get_checkout(session.provider_ref)).status is CheckoutStatus.EXPIRED
    assert await provider.expire_checkout(session.provider_ref) is False
    # The same attempt asked again gets its session as it stands: expired.
    assert (await provider.create_checkout(CHECKOUT)).status is CheckoutStatus.EXPIRED


async def test_a_paid_session_refuses_to_expire_and_reads_back_complete(
    payment_provider: PaymentProviderBackend,
) -> None:
    provider = payment_provider.provider
    session = await provider.create_checkout(CHECKOUT)

    paid = await payment_provider.pay(session.provider_ref)

    assert paid.status is CheckoutStatus.COMPLETE
    assert paid.payment_ref is not None
    assert paid.payment_status is CheckoutPaymentStatus.PAID
    assert await provider.expire_checkout(session.provider_ref) is False
    assert await provider.get_checkout(session.provider_ref) == paid


async def test_a_session_expired_first_can_no_longer_be_paid(
    payment_provider: PaymentProviderBackend,
) -> None:
    provider = payment_provider.provider
    session = await provider.create_checkout(CHECKOUT)
    await provider.expire_checkout(session.provider_ref)

    after = await payment_provider.pay(session.provider_ref)

    assert after.status is CheckoutStatus.EXPIRED
    assert after.payment_ref is None
    assert after.payment_status is not CheckoutPaymentStatus.PAID


async def test_an_unknown_session_is_not_found(payment_provider: PaymentProviderBackend) -> None:
    provider = payment_provider.provider

    with pytest.raises(RecordNotFound):
        await provider.get_checkout("cs_404")
    with pytest.raises(RecordNotFound):
        await provider.expire_checkout("cs_404")


def _event_of(provider: PaymentProvider) -> PaymentEvent:
    """EVENT, as the provider under test sends it."""
    return EVENT.model_copy(update={"provider": provider.name})


async def test_a_signed_webhook_verifies_into_its_event(
    payment_provider: PaymentProviderBackend,
) -> None:
    event = _event_of(payment_provider.provider)
    headers, body = payment_provider.signed(event)

    assert await payment_provider.provider.verify_webhook(headers, body) == event


async def test_a_forged_webhook_is_refused_before_its_body_is_read(
    payment_provider: PaymentProviderBackend,
) -> None:
    provider = payment_provider.provider
    headers, body = payment_provider.signed(_event_of(provider))
    forged_headers, forged_body = payment_provider.forged(_event_of(provider))

    with pytest.raises(InvalidSignature):
        await provider.verify_webhook(headers, body.replace(b"2500", b"1"))
    with pytest.raises(InvalidSignature):
        await provider.verify_webhook(forged_headers, forged_body)
    with pytest.raises(InvalidSignature):
        await provider.verify_webhook({}, b"not even json")


async def test_a_verified_body_that_is_no_event_of_its_own_is_refused(
    payment_provider: PaymentProviderBackend,
) -> None:
    headers, body = payment_provider.foreign()

    with pytest.raises(ValueError):  # noqa: PT011 - each provider words its refusal
        await payment_provider.provider.verify_webhook(headers, body)


async def test_a_provider_that_cannot_be_reached_is_unavailable(
    payment_provider: PaymentProviderBackend,
) -> None:
    provider = payment_provider.unreachable()

    with pytest.raises(ServiceUnavailable):
        await provider.create_checkout(CHECKOUT)
    with pytest.raises(ServiceUnavailable):
        await provider.expire_checkout("cs_1")
    with pytest.raises(ServiceUnavailable):
        await provider.get_checkout("cs_1")
