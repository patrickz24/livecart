from itertools import pairwise, product

import pytest

from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.shipments import IN_FLIGHT, SHIPMENT_ROUTE, shipment_steps
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


@pytest.mark.parametrize(
    ("current", "reported", "steps"),
    [
        (ShipmentStatus.CREATED, ShipmentStatus.CREATED, ()),
        (ShipmentStatus.CREATED, ShipmentStatus.IN_TRANSIT, (ShipmentStatus.IN_TRANSIT,)),
        (
            ShipmentStatus.CREATED,
            ShipmentStatus.OUT_FOR_DELIVERY,
            (ShipmentStatus.IN_TRANSIT, ShipmentStatus.OUT_FOR_DELIVERY),
        ),
        (ShipmentStatus.CREATED, ShipmentStatus.DELIVERED, SHIPMENT_ROUTE[1:]),
        (
            ShipmentStatus.IN_TRANSIT,
            ShipmentStatus.DELIVERED,
            (ShipmentStatus.OUT_FOR_DELIVERY, ShipmentStatus.DELIVERED),
        ),
        (ShipmentStatus.OUT_FOR_DELIVERY, ShipmentStatus.DELIVERED, (ShipmentStatus.DELIVERED,)),
        (ShipmentStatus.CREATED, ShipmentStatus.CANCELLED, (ShipmentStatus.CANCELLED,)),
        (ShipmentStatus.DELIVERED, ShipmentStatus.DELIVERED, ()),
        (ShipmentStatus.CANCELLED, ShipmentStatus.CANCELLED, ()),
    ],
)
def test_a_reported_status_is_reached_one_transition_at_a_time(
    current: ShipmentStatus, reported: ShipmentStatus, steps: tuple[ShipmentStatus, ...]
) -> None:
    assert shipment_steps(current, reported) == steps


def test_every_step_is_one_transition_of_the_machine() -> None:
    for current, reported in product(ShipmentStatus, repeat=2):
        try:
            steps = shipment_steps(current, reported)
        except InvalidTransition:
            continue
        path = (current, *steps)
        assert set(pairwise(path)) <= SHIPMENT_TRANSITIONS
        assert path[-1] is reported


@pytest.mark.parametrize(
    ("current", "reported"),
    [
        (ShipmentStatus.IN_TRANSIT, ShipmentStatus.CREATED),
        (ShipmentStatus.DELIVERED, ShipmentStatus.OUT_FOR_DELIVERY),
        (ShipmentStatus.IN_TRANSIT, ShipmentStatus.CANCELLED),
        (ShipmentStatus.DELIVERED, ShipmentStatus.CANCELLED),
        (ShipmentStatus.CANCELLED, ShipmentStatus.IN_TRANSIT),
        (ShipmentStatus.CANCELLED, ShipmentStatus.CREATED),
    ],
)
def test_a_status_the_machine_cannot_reach_is_refused(
    current: ShipmentStatus, reported: ShipmentStatus
) -> None:
    with pytest.raises(InvalidTransition):
        shipment_steps(current, reported)
