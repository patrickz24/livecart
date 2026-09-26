"""Viva Wallet Smart Checkout: a documented stub.

How the real adapter would map Viva onto ``PaymentProvider``, as Viva's
developer documentation describes Smart Checkout at the time of writing
(to check again when the adapter is written):

- ``create_checkout``: ``POST /checkout/v2/orders`` with an OAuth 2.0 access
  token (client credentials of Viva's accounts service), the amount in minor
  units, the order's reference in ``merchantTrns`` and a payment timeout
  that outlasts the reservation. The answer's ``orderCode`` is the provider
  ref, and the redirect URL is Viva's checkout page with ``ref=<orderCode>``.
  Viva takes no idempotency key: the adapter would read the attempt's order
  code back before it creates another, so that a replayed attempt reuses it;
- Viva sends the buyer back to the success and failure URLs of the payment
  source configured in Viva's dashboard, not per request: that source would
  point them at the bridge page;
- ``expire_checkout``: cancel the payment order by its code while it is
  unpaid, which Viva refuses once a transaction has paid it: expire-first;
- ``get_checkout``: read the order's transactions back, a successful one
  meaning complete;
- ``verify_webhook``: Viva's webhooks carry no HMAC header. The endpoint
  first proves itself by answering Viva's ``GET`` with the verification key
  the account's API gives, and each event, such as a transaction payment
  created, is then trusted only once the adapter has read the same
  transaction back through the API: the body alone proves nothing.
"""

from typing import ClassVar

from livecart_core.application.dto import PaymentProviderName
from livecart_core.infrastructure.payments.stub import PaymentProviderStub


class VivaPaymentProvider(PaymentProviderStub):
    """Refuses to be built: see the module docstring for the flow to implement."""

    # None: an event is confirmed by reading its transaction back instead.
    signature_headers: ClassVar[tuple[str, ...]] = ()
    _provider: ClassVar[PaymentProviderName] = PaymentProviderName.VIVA
