"""PayPal Checkout, through the Orders v2 REST API: a documented stub.

How the real adapter would map PayPal onto ``PaymentProvider``, every call
authenticated by an OAuth 2.0 access token of the app's client id and secret
(client credentials):

- ``create_checkout``: ``POST /v2/checkout/orders`` with ``intent: CAPTURE``,
  one purchase unit of the order's total (a currency code and a decimal
  string, converted from the minor units) whose ``invoice_id`` is the order's
  reference, the bridge page as ``return_url`` and ``cancel_url``, and the
  attempt's idempotency key in the ``PayPal-Request-Id`` header. The order's
  id is the provider ref, and its ``payer-action`` link the redirect URL;
- the buyer approving on PayPal moves no money: the order is ``APPROVED``,
  and only LiveCart's own ``POST /v2/checkout/orders/{id}/capture`` charges
  it, once the ``CHECKOUT.ORDER.APPROVED`` webhook arrives;
- ``expire_checkout``: Orders v2 has no call that cancels an order, and none
  is needed: an order nobody captures is never charged. The adapter would
  record the order as not to be captured, and refuse (``False``) once the
  capture has happened, so that expire-first holds because LiveCart is the
  one that captures;
- ``get_checkout``: ``GET /v2/checkout/orders/{id}``, ``CREATED`` and
  ``APPROVED`` read as open, ``COMPLETED`` as complete;
- ``verify_webhook``: PayPal signs each delivery with the headers below. The
  adapter checks them before it reads the body, either through ``POST
  /v1/notifications/verify-webhook-signature`` with the webhook's id, or
  offline, with the certificate that ``PAYPAL-CERT-URL`` names (served by
  PayPal's own host, which the adapter checks first), over
  ``<transmission id>|<transmission time>|<webhook id>|<CRC32 of the body>``.
  ``PAYMENT.CAPTURE.COMPLETED`` then maps onto a completed checkout, and
  ``PAYMENT.CAPTURE.DENIED`` onto a failed one.
"""

from typing import ClassVar

from livecart_core.application.dto import PaymentProviderName
from livecart_core.infrastructure.payments.stub import PaymentProviderStub


class PayPalPaymentProvider(PaymentProviderStub):
    """Refuses to be built: see the module docstring for the flow to implement."""

    signature_headers: ClassVar[tuple[str, ...]] = (
        "PAYPAL-TRANSMISSION-ID",
        "PAYPAL-TRANSMISSION-TIME",
        "PAYPAL-TRANSMISSION-SIG",
        "PAYPAL-CERT-URL",
        "PAYPAL-AUTH-ALGO",
    )
    _provider: ClassVar[PaymentProviderName] = PaymentProviderName.PAYPAL
