"""Chronopost, through its SOAP web services: a documented stub.

How the real adapter would map Chronopost onto ``CarrierProvider``, as
Chronopost's web services documentation describes them at the time of
writing (to check again, operation versions included, when the adapter is
written). Every call carries the account number and its password; there is
no token to keep:

- ``create_shipment``: the shipping operation of ``ShippingServiceWS``, with
  the shipper, the recipient from the ``ShippingAddress``, the product code
  of the contract and one parcel whose reference is the order's reference.
  The answer gives the skybill number, which is both the carrier ref and the
  tracking number, and the label (the skybill, as a PDF) for the seller to
  print. Chronopost takes no idempotency key: the adapter would record the
  order's skybill number before it asks for another, and reuse it;
- ``get_tracking``: ``TrackingServiceWS.trackSkybillV2`` with the skybill
  number returns the parcel's events, each with a code, a label and a date.
  Before the first event the parcel is ``created``; the taking in charge and
  the hub scans map onto ``in_transit``, the delivery round onto
  ``out_for_delivery``, and the delivery onto ``delivered``;
- ``cancel_shipment``: ``TrackingServiceWS.cancelSkybill`` with the skybill
  number, which Chronopost accepts until the parcel is taken in charge; once
  it refuses, the adapter raises ``InvalidTransition``.
"""

from typing import ClassVar

from livecart_core.application.dto import CarrierName
from livecart_core.infrastructure.carriers.stub import CarrierProviderStub


class ChronopostCarrier(CarrierProviderStub):
    """Refuses to be built: see the module docstring for the flow to implement."""

    _carrier: ClassVar[CarrierName] = CarrierName.CHRONOPOST
