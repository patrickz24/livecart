"""The route of a shipment, as its carrier moves it from the label to the buyer's door.

A paid order's parcel goes ``created`` (the label exists, the carrier has not
picked the parcel up), ``in_transit``, ``out_for_delivery``, ``delivered``,
each status the only source of the next one in ``SHIPMENT_TRANSITIONS``. A
shipment may also be cancelled, but only before the carrier picks it up.
"""

from typing import Final

from livecart_core.domain.state_machines import ShipmentStatus

# The statuses of a delivery, in the order the carrier reaches them.
SHIPMENT_ROUTE: Final[tuple[ShipmentStatus, ...]] = (
    ShipmentStatus.CREATED,
    ShipmentStatus.IN_TRANSIT,
    ShipmentStatus.OUT_FOR_DELIVERY,
    ShipmentStatus.DELIVERED,
)
