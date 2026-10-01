"""``CarrierProvider`` on every backend: the in-memory fake, then the stateless mock.

One shipment per order, named the same whatever the calls; a route walked
forward, one step at a time, to ``delivered`` for good; a cancellation
accepted only before the carrier picks the parcel up; and a ref the carrier
never issued, unknown.
"""

from uuid import UUID

import pytest

from livecart_core.application.dto import ShipmentRequest, ShippingAddress, TrackingUpdate
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.reference import OrderReference
from livecart_core.domain.shipments import SHIPMENT_ROUTE
from livecart_core.domain.state_machines import ShipmentStatus
from livecart_core.testing.backends import CarrierBackend

ADDRESS = ShippingAddress(
    full_name="Camille Martin",
    line1="12 rue des Lilas",
    postal_code="69003",
    city="Lyon",
    country="FR",
)


def _parcel(order: int = 10) -> ShipmentRequest:
    return ShipmentRequest(UUID(int=order), OrderReference("LC-7K3F9Q"), ADDRESS)


async def test_a_shipment_starts_created_at_the_carriers_time(
    carrier_provider: CarrierBackend,
) -> None:
    carrier = carrier_provider.carrier

    ref = await carrier.create_shipment(_parcel())

    assert ref.carrier is carrier.name
    assert ref.created_at == carrier_provider.clock.now()
    assert ref.tracking_number
    assert await carrier.get_tracking(ref) == TrackingUpdate(ShipmentStatus.CREATED, ref.created_at)


async def test_an_order_names_one_shipment_whatever_the_calls(
    carrier_provider: CarrierBackend,
) -> None:
    carrier = carrier_provider.carrier

    first = await carrier.create_shipment(_parcel(10))
    carrier_provider.clock.advance(30)
    again = await carrier.create_shipment(_parcel(10))
    other = await carrier.create_shipment(_parcel(11))

    assert (again.carrier_ref, again.tracking_number) == (first.carrier_ref, first.tracking_number)
    assert other.carrier_ref != first.carrier_ref
    assert other.tracking_number != first.tracking_number


async def test_a_shipment_walks_its_route_one_step_at_a_time_to_delivered(
    carrier_provider: CarrierBackend,
) -> None:
    carrier = carrier_provider.carrier
    clock = carrier_provider.clock
    ref = await carrier.create_shipment(_parcel())

    reported = []
    for _ in SHIPMENT_ROUTE[1:]:
        await carrier_provider.carry(ref)
        reported.append(await carrier.get_tracking(ref))
    delivered_at = clock.now()
    await carrier_provider.carry(ref)

    assert reported == [
        TrackingUpdate(status, ref.created_at + step * carrier_provider.step)
        for step, status in enumerate(SHIPMENT_ROUTE[1:], start=1)
    ]
    # Delivered for good, at the time it was delivered.
    assert await carrier.get_tracking(ref) == TrackingUpdate(ShipmentStatus.DELIVERED, delivered_at)


async def test_only_a_shipment_not_picked_up_yet_can_be_cancelled(
    carrier_provider: CarrierBackend,
) -> None:
    carrier = carrier_provider.carrier
    waiting = await carrier.create_shipment(_parcel(10))
    moving = await carrier.create_shipment(_parcel(11))

    await carrier.cancel_shipment(waiting)
    await carrier.cancel_shipment(waiting)
    await carrier_provider.carry(moving)

    assert (await carrier.get_tracking(moving)).status is ShipmentStatus.IN_TRANSIT
    with pytest.raises(InvalidTransition):
        await carrier.cancel_shipment(moving)


async def test_a_ref_the_carrier_never_issued_is_not_found(
    carrier_provider: CarrierBackend,
) -> None:
    carrier = carrier_provider.carrier
    foreign = await carrier_provider.foreign()

    with pytest.raises(RecordNotFound):
        await carrier.get_tracking(foreign)
    with pytest.raises(RecordNotFound):
        await carrier.cancel_shipment(foreign)
