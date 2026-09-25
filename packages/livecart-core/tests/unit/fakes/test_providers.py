from dataclasses import replace
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    CarrierName,
    CheckoutRequest,
    CheckoutStatus,
    PaymentEvent,
    PaymentProviderName,
    ShipmentRequest,
    ShippingAddress,
    TrackingUpdate,
)
from livecart_core.application.ports import InvalidSignature, RecordNotFound, ServiceUnavailable
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.money import Money
from livecart_core.domain.reference import OrderReference
from livecart_core.domain.state_machines import ShipmentStatus
from livecart_core.testing.fakes import (
    FAKE_CLOCK_START,
    FakeCarrierProvider,
    FakeClock,
    FakePaymentProvider,
)

ORDER_ID = UUID(int=10)
CHECKOUT = CheckoutRequest(
    ORDER_ID,
    1,
    OrderReference("LC-7K3F9Q"),
    Money(2_500, "EUR"),
    "https://example.invalid/success",
    "https://example.invalid/cancel",
)
EVENT = PaymentEvent(
    provider=PaymentProviderName.MOCK,
    event_id="evt_1",
    event_type="checkout.session.completed",
    checkout_ref="fake_cs_1",
    amount_minor=2_500,
    currency="EUR",
    occurred_at=FAKE_CLOCK_START,
)
ADDRESS = ShippingAddress(
    full_name="Camille Martin",
    line1="12 rue des Lilas",
    postal_code="69003",
    city="Lyon",
    country="FR",
)


# --- Payment provider ------------------------------------------------------------------


async def test_a_checkout_opens_a_session_for_the_amount() -> None:
    provider = FakePaymentProvider()

    session = await provider.create_checkout(CHECKOUT)

    assert provider.name is PaymentProviderName.MOCK
    assert session.status is CheckoutStatus.OPEN
    assert session.amount == Money(2_500, "EUR")
    assert session.redirect_url.endswith(session.provider_ref)
    assert await provider.get_checkout(session.provider_ref) == session


async def test_the_same_attempt_always_gets_the_same_session() -> None:
    provider = FakePaymentProvider()

    first = await provider.create_checkout(CHECKOUT)
    again = await provider.create_checkout(CHECKOUT)
    second_attempt = await provider.create_checkout(replace(CHECKOUT, attempt_no=2))

    assert again == first
    assert second_attempt.provider_ref != first.provider_ref
    assert len(provider.requests) == 3


async def test_a_reused_key_with_other_parameters_is_refused() -> None:
    provider = FakePaymentProvider()
    await provider.create_checkout(CHECKOUT)

    with pytest.raises(ValueError, match="reused with other parameters"):
        await provider.create_checkout(replace(CHECKOUT, amount=Money(1, "EUR")))


async def test_only_an_open_session_can_be_expired() -> None:
    provider = FakePaymentProvider()
    session = await provider.create_checkout(CHECKOUT)

    assert await provider.expire_checkout(session.provider_ref) is True
    assert (await provider.get_checkout(session.provider_ref)).status is CheckoutStatus.EXPIRED
    assert await provider.expire_checkout(session.provider_ref) is False


async def test_a_paid_session_refuses_to_expire_and_reads_back_complete() -> None:
    provider = FakePaymentProvider()
    session = await provider.create_checkout(CHECKOUT)

    provider.complete(session.provider_ref, payment_ref="fake_pi_1")

    assert await provider.expire_checkout(session.provider_ref) is False
    paid = await provider.get_checkout(session.provider_ref)
    assert paid.status is CheckoutStatus.COMPLETE
    assert paid.payment_ref == "fake_pi_1"
    with pytest.raises(ValueError, match="not open"):
        provider.complete(session.provider_ref)


async def test_an_unknown_session_is_not_found() -> None:
    provider = FakePaymentProvider()

    with pytest.raises(RecordNotFound):
        await provider.get_checkout("fake_cs_404")
    with pytest.raises(RecordNotFound):
        await provider.expire_checkout("fake_cs_404")


async def test_a_signed_webhook_verifies_into_its_event() -> None:
    provider = FakePaymentProvider()
    headers, body = provider.signed_webhook(EVENT)

    assert await provider.verify_webhook(headers, body) == EVENT


async def test_a_forged_webhook_is_refused_before_its_body_is_read() -> None:
    provider = FakePaymentProvider()
    headers, body = provider.signed_webhook(EVENT)
    other_headers, _ = FakePaymentProvider(key=b"another-key").signed_webhook(EVENT)

    with pytest.raises(InvalidSignature):
        await provider.verify_webhook(headers, body.replace(b"2500", b"1"))
    with pytest.raises(InvalidSignature):
        await provider.verify_webhook(other_headers, body)
    with pytest.raises(InvalidSignature):
        await provider.verify_webhook({}, b"not even json")


async def test_provider_calls_fail_on_demand() -> None:
    provider = FakePaymentProvider(PaymentProviderName.STRIPE)
    provider.fail_next(3)

    with pytest.raises(ServiceUnavailable):
        await provider.create_checkout(CHECKOUT)
    with pytest.raises(ServiceUnavailable):
        await provider.expire_checkout("fake_cs_1")
    with pytest.raises(ServiceUnavailable):
        await provider.get_checkout("fake_cs_1")
    assert provider.requests == []
    assert (await provider.create_checkout(CHECKOUT)).provider_ref == "fake_cs_1"


# --- Carrier ---------------------------------------------------------------------------


def _shipment(order: int = 10) -> ShipmentRequest:
    return ShipmentRequest(UUID(int=order), OrderReference("LC-7K3F9Q"), ADDRESS)


async def test_a_shipment_starts_created_at_the_clock_time() -> None:
    clock = FakeClock()
    carrier = FakeCarrierProvider(clock)

    ref = await carrier.create_shipment(_shipment())

    assert carrier.name is CarrierName.MOCK
    assert ref.carrier is CarrierName.MOCK
    assert ref.created_at == FAKE_CLOCK_START
    assert await carrier.get_tracking(ref) == TrackingUpdate(
        ShipmentStatus.CREATED, FAKE_CLOCK_START
    )


async def test_one_order_has_one_shipment() -> None:
    carrier = FakeCarrierProvider(FakeClock())

    first = await carrier.create_shipment(_shipment(10))
    again = await carrier.create_shipment(_shipment(10))
    other = await carrier.create_shipment(_shipment(11))

    assert again == first
    assert other.tracking_number != first.tracking_number
    assert other.carrier_ref != first.carrier_ref


async def test_a_shipment_moves_one_step_at_a_time() -> None:
    clock = FakeClock()
    carrier = FakeCarrierProvider(clock)
    ref = await carrier.create_shipment(_shipment())

    clock.advance(120)
    carrier.advance(ref, ShipmentStatus.IN_TRANSIT)
    unchanged = carrier.advance(ref, ShipmentStatus.IN_TRANSIT)

    assert await carrier.get_tracking(ref) == TrackingUpdate(ShipmentStatus.IN_TRANSIT, clock.now())
    assert unchanged.status is ShipmentStatus.IN_TRANSIT
    with pytest.raises(InvalidTransition):
        carrier.advance(ref, ShipmentStatus.DELIVERED)


async def test_only_a_shipment_not_yet_in_transit_can_be_cancelled() -> None:
    carrier = FakeCarrierProvider(FakeClock(), CarrierName.DPD)
    waiting = await carrier.create_shipment(_shipment(10))
    moving = await carrier.create_shipment(_shipment(11))
    carrier.advance(moving, ShipmentStatus.IN_TRANSIT)

    await carrier.cancel_shipment(waiting)
    await carrier.cancel_shipment(waiting)

    assert (await carrier.get_tracking(waiting)).status is ShipmentStatus.CANCELLED
    with pytest.raises(InvalidTransition):
        await carrier.cancel_shipment(moving)


async def test_an_unknown_shipment_is_not_found() -> None:
    carrier = FakeCarrierProvider(FakeClock())
    known = await FakeCarrierProvider(FakeClock()).create_shipment(_shipment())

    with pytest.raises(RecordNotFound):
        await carrier.get_tracking(known)


async def test_carrier_calls_fail_on_demand() -> None:
    carrier = FakeCarrierProvider(FakeClock())
    ref = await carrier.create_shipment(_shipment())
    carrier.fail_next(3)

    with pytest.raises(ServiceUnavailable):
        await carrier.create_shipment(_shipment(11))
    with pytest.raises(ServiceUnavailable):
        await carrier.get_tracking(ref)
    with pytest.raises(ServiceUnavailable):
        await carrier.cancel_shipment(ref)
    assert (await carrier.get_tracking(ref)).status is ShipmentStatus.CREATED
