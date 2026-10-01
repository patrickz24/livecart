"""DHL, through its REST APIs on the DHL developer portal: a documented stub.

DHL ships parcels through several products, each with its own API. This
follows DHL Parcel Germany's shipping API and DHL's unified tracking API, as
the portal describes them at the time of writing (to check again when the
adapter is written); DHL Express would go through the MyDHL API instead,
whose calls map the same way:

- authentication: the app's API key in a header on every call, plus, for
  the shipping API, the business customer account's user and password;
- ``create_shipment``: ``POST /orders`` of the Parcel DE Shipping API (v2)
  with one shipment: the account's billing number, the shipper, the
  consignee from the ``ShippingAddress``, the parcel's weight, and the
  order's reference as the shipment's reference. The answer gives the
  shipment number, which is both the carrier ref and the tracking number,
  and the label to print. The adapter would read the shipment of the
  order's reference back before it creates another, so that a replayed
  creation finds the first;
- ``get_tracking``: ``GET /track/shipments?trackingNumber=<number>`` of the
  unified Shipment Tracking API, whose status codes are ``pre-transit``,
  ``transit``, ``delivered``, ``failure`` and ``unknown``, with the events
  behind them. ``pre-transit`` maps onto ``created``, ``transit`` onto
  ``in_transit``, an event of the delivery round onto ``out_for_delivery``,
  and ``delivered`` onto ``delivered``; a ``failure`` (an address not found,
  a parcel returned) needs a person, and the adapter would raise. DHL's push
  notifications could replace the worker's polling;
- ``cancel_shipment``: ``DELETE /orders?shipment=<number>`` of the shipping
  API, which DHL accepts until the day's manifest is closed; once it
  refuses, the adapter raises ``InvalidTransition``.
"""

from typing import ClassVar

from livecart_core.application.dto import CarrierName
from livecart_core.infrastructure.carriers.stub import CarrierProviderStub


class DhlCarrier(CarrierProviderStub):
    """Refuses to be built: see the module docstring for the flow to implement."""

    _carrier: ClassVar[CarrierName] = CarrierName.DHL
