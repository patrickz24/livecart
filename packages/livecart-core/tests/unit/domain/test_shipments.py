from itertools import pairwise

from livecart_core.domain.shipments import SHIPMENT_ROUTE
from livecart_core.domain.state_machines import SHIPMENT_TRANSITIONS, ShipmentStatus


def test_the_route_walks_the_machine_from_created_to_delivered() -> None:
    assert SHIPMENT_ROUTE[0] is ShipmentStatus.CREATED
    assert SHIPMENT_ROUTE[-1] is ShipmentStatus.DELIVERED
    assert set(pairwise(SHIPMENT_ROUTE)) <= SHIPMENT_TRANSITIONS


def test_every_move_off_the_route_is_a_cancellation_before_pickup() -> None:
    assert SHIPMENT_TRANSITIONS - set(pairwise(SHIPMENT_ROUTE)) == {
        (ShipmentStatus.CREATED, ShipmentStatus.CANCELLED)
    }
