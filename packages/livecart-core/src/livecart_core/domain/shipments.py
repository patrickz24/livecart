"""The route of a shipment, as its carrier moves it from the label to the buyer's door.

A paid order's parcel goes ``created`` (the label exists, the carrier has not
picked the parcel up), ``in_transit``, ``out_for_delivery``, ``delivered``,
each status the only source of the next one in ``SHIPMENT_TRANSITIONS``. A
shipment may also be cancelled, but only before the carrier picks it up.
``shipment_steps`` turns what a carrier reports into the transitions that
lead there.
"""

from typing import Final

from livecart_core.domain.state_machines import SHIPMENT_TRANSITIONS, ShipmentStatus, transition

# The statuses of a delivery, in the order the carrier reaches them.
SHIPMENT_ROUTE: Final[tuple[ShipmentStatus, ...]] = (
    ShipmentStatus.CREATED,
    ShipmentStatus.IN_TRANSIT,
    ShipmentStatus.OUT_FOR_DELIVERY,
    ShipmentStatus.DELIVERED,
)
# The statuses of a shipment still on its way, neither delivered nor cancelled:
# what the tracking polls the carrier about.
IN_FLIGHT: Final[tuple[ShipmentStatus, ...]] = SHIPMENT_ROUTE[:-1]


def shipment_steps(current: ShipmentStatus, reported: ShipmentStatus) -> tuple[ShipmentStatus, ...]:
    """The transitions that bring a shipment from ``current`` to the status its carrier reports.

    One step of the machine each, in order, and none when the shipment is
    there already. A carrier reports where a shipment is, not every step it
    took: a shipment found two steps further on its route walks through the
    step in between, so that each transition is applied, and told, once.
    Raises ``InvalidTransition`` for a status the machine cannot reach from
    ``current``: back along the route, or a cancellation once in transit.
    """
    if current == reported:
        return ()
    if current in SHIPMENT_ROUTE and reported in SHIPMENT_ROUTE:
        start, end = SHIPMENT_ROUTE.index(current), SHIPMENT_ROUTE.index(reported)
        if start < end:
            return SHIPMENT_ROUTE[start + 1 : end + 1]
    # Off the route, the only move is the one the table allows, or none.
    transition(SHIPMENT_TRANSITIONS, current, reported)
    return (reported,)
