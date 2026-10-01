"""DPD, through its SOAP web services: a documented stub.

DPD's APIs differ by country. This follows Web Connect, the services DPD
Germany documents (to check again, and against the account's own country,
when the adapter is written); a French account goes through DPD France's
own web services, whose calls differ but map the same way:

- authentication: ``LoginService.getAuth`` with the account's DELIS id and
  password returns an ``authToken`` valid for a day, sent in the header of
  every later call; the adapter would keep it and log in again once refused;
- ``create_shipment``: ``ShipmentService.storeOrders`` with one order, its
  general data (the sending depot, the product, such as Classic, the sender
  and the recipient from the ``ShippingAddress``) and one parcel whose
  customer reference is the order's reference. The answer gives the
  parcel's label number, which is both the carrier ref and the tracking
  number, and the label as a PDF for the seller to print. DPD takes no
  idempotency key: the adapter would search for a parcel of the same
  customer reference before it stores another, so that a replayed creation
  finds the first;
- ``get_tracking``: ``ParcelLifeCycleService.getTrackingData`` with the label
  number returns the parcel's status history, the current one flagged. Before
  the first scan the parcel is ``created``; the depot and line-haul scans
  map onto ``in_transit``, the delivery round onto ``out_for_delivery``, and
  the delivery onto ``delivered``;
- ``cancel_shipment``: how a stored parcel is cancelled depends on the
  account (a parcel never handed over is simply not picked up): the adapter
  would accept the cancellation before the first scan, and refuse it with
  ``InvalidTransition`` once the parcel is in DPD's hands.
"""

from typing import ClassVar

from livecart_core.application.dto import CarrierName
from livecart_core.infrastructure.carriers.stub import CarrierProviderStub


class DpdCarrier(CarrierProviderStub):
    """Refuses to be built: see the module docstring for the flow to implement."""

    _carrier: ClassVar[CarrierName] = CarrierName.DPD
