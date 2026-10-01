from itertools import pairwise

from livecart_core.domain.shipments import IN_FLIGHT, SHIPMENT_ROUTE
from livecart_core.domain.state_machines import SHIPMENT_TRANSITIONS, ShipmentStatus


def test_the_route_walks_the_machine_from_created_to_delivered() -> None:
    assert SHIPMENT_ROUTE[0] is ShipmentStatus.CREATED
    assert SHIPMENT_ROUTE[-1] is ShipmentStatus.DELIVERED
    assert set(pairwise(SHIPMENT_ROUTE)) <= SHIPMENT_TRANSITIONS


def test_every_move_off_the_route_is_a_cancellation_before_pickup() -> None:
    assert SHIPMENT_TRANSITIONS - set(pairwise(SHIPMENT_ROUTE)) == {
        (ShipmentStatus.CREATED, ShipmentStatus.CANCELLED)
    }


def test_a_shipment_is_in_flight_until_a_terminal_status() -> None:
    sources = {current for current, _ in SHIPMENT_TRANSITIONS}

    assert set(IN_FLIGHT) == sources
    assert set(ShipmentStatus) - set(IN_FLIGHT) == {
        ShipmentStatus.DELIVERED,
        ShipmentStatus.CANCELLED,
    }
