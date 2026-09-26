"""``ReceivePaymentWebhook``: a provider's payment webhook, verified, then recorded for the worker.

Step 13 of the plan's journey, its first half. The provider (Stripe, or the
mock's hosted page standing for it) posts each event of a checkout session
to the payment webhook, and retries until it gets a 2xx. In this order:

1. ``PaymentProvider.verify_webhook`` checks the signature on the raw body
   before anything else reads it (``construct_event`` for Stripe, the HMAC of
   ``livecart_core.signing`` for the mock), then reduces the event to its
   allow-list, ``PaymentEvent``: the customer's details never go further;
2. ``WebhookEventRecorder.record`` inserts it into the durable registry,
   ``processed_webhook_events``, once per (provider, event_id): a redelivery
   is counted, not recorded again.

That is all the webhook does: applying the event to its payment and its
order is the worker's, which polls the registry (``ProcessPaymentEvents``),
so that the provider is acknowledged as soon as the event is durable, and
never waits for the order's lock. Nothing on this path needs Redis.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from livecart_core.application.dto import PaymentEvent, PaymentProviderName
from livecart_core.application.ports import Clock, PaymentProvider, WebhookEventRecorder


@dataclass(frozen=True, slots=True)
class ReceivedWebhook:
    """A verified event now in the registry, and whether an earlier delivery had put it there."""

    event: PaymentEvent
    redelivery: bool


class ReceivePaymentWebhook:
    """One delivery of the provider's payment webhook; see the module docstring."""

    def __init__(
        self, provider: PaymentProvider, events: WebhookEventRecorder, clock: Clock
    ) -> None:
        self._provider = provider
        self._events = events
        self._clock = clock

    @property
    def provider(self) -> PaymentProviderName:
        """The provider whose webhook this is: the one ``PAYMENT_PROVIDER`` selects."""
        return self._provider.name

    async def __call__(self, headers: Mapping[str, str], raw_body: bytes) -> ReceivedWebhook:
        """Verify, then record.

        Raises ``InvalidSignature`` before the body is read when the signature
        does not hold, ``ValueError`` for a verified body that is no event of
        the provider, and ``ServiceUnavailable`` when the registry cannot take
        it: the provider delivers it again later.
        """
        event = await self._provider.verify_webhook(headers, raw_body)
        first = await self._events.record(event, now=self._clock.now())
        return ReceivedWebhook(event, redelivery=not first)
