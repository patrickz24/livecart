"""The shape shared by the carriers that are documented but not implemented.

DPD, Chronopost and DHL are out of the MVP's scope. Each still has its class,
which names the carrier and documents how its API's shipments, tracking and
cancellations map onto the three calls of ``CarrierProvider``, so that the
real adapter knows what it must do. Building one raises
``NotImplementedError``: selected by ``CARRIER_PROVIDER``, a stub stops the
worker at startup rather than leave paid orders without a parcel.
"""

from typing import ClassVar

from livecart_core.application.dto import (
    CarrierName,
    ShipmentRef,
    ShipmentRequest,
    TrackingUpdate,
)


class CarrierProviderStub:
    """A carrier that is designed but not implemented; see the module docstring."""

    _carrier: ClassVar[CarrierName]

    def __init__(self) -> None:
        raise NotImplementedError(
            f"the {self._carrier} carrier is a documented stub, not implemented: "
            "choose another CARRIER_PROVIDER"
        )

    @property
    def name(self) -> CarrierName:
        return self._carrier

    async def create_shipment(self, request: ShipmentRequest) -> ShipmentRef:
        raise NotImplementedError(f"the {self._carrier} carrier is a stub")

    async def get_tracking(self, ref: ShipmentRef) -> TrackingUpdate:
        raise NotImplementedError(f"the {self._carrier} carrier is a stub")

    async def cancel_shipment(self, ref: ShipmentRef) -> None:
        raise NotImplementedError(f"the {self._carrier} carrier is a stub")
