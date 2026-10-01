"""What only the provider fakes promise: the requests they record and the failures they script.

What every payment provider does, the fake and the mock alike, is in
``tests/contract/test_payment_provider.py``, and what every carrier does in
``tests/contract/test_carrier_provider.py``.
"""

from dataclasses import replace
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    CarrierName,
    CheckoutRequest,
    PaymentProviderName,
    ShipmentRequest,
    ShippingAddress,
    TrackingUpdate,
)
from livecart_core.application.ports import RecordNotFound, ServiceUnavailable
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.money import Money
from livecart_core.domain.reference import OrderReference
from livecart_core.domain.state_machines import ShipmentStatus
from livecart_core.testing.fakes import (
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
ADDRESS = ShippingAddress(
    full_name="Camille Martin",
    line1="12 rue des Lilas",
    postal_code="69003",
    city="Lyon",
    country="FR",
)


# --- Payment provider ------------------------------------------------------------------


async def test_the_fake_records_every_checkout_request() -> None:
    provider = FakePaymentProvider()

    await provider.create_checkout(CHECKOUT)
    await provider.create_checkout(CHECKOUT)
    await provider.create_checkout(replace(CHECKOUT, attempt_no=2))

    assert provider.requests == [CHECKOUT, CHECKOUT, replace(CHECKOUT, attempt_no=2)]


async def test_only_an_open_session_can_be_completed() -> None:
    provider = FakePaymentProvider()
    session = await provider.create_checkout(CHECKOUT)

    paid = provider.complete(session.provider_ref, payment_ref="fake_pi_1")

    assert paid.payment_ref == "fake_pi_1"
    with pytest.raises(ValueError, match="not open"):
        provider.complete(session.provider_ref)
    with pytest.raises(RecordNotFound):
        provider.complete("fake_cs_404")


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


async def test_the_fake_reports_the_cancellation_it_accepted() -> None:
    carrier = FakeCarrierProvider(FakeClock(), CarrierName.DPD)
    waiting = await carrier.create_shipment(_shipment(10))
    moving = await carrier.create_shipment(_shipment(11))
    carrier.advance(moving, ShipmentStatus.IN_TRANSIT)

    await carrier.cancel_shipment(waiting)
    await carrier.cancel_shipment(waiting)

    assert (await carrier.get_tracking(waiting)).status is ShipmentStatus.CANCELLED
    with pytest.raises(InvalidTransition):
        await carrier.cancel_shipment(moving)


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
