"""The registry of payment webhooks on every backend: the in-memory fake, then PostgreSQL.

One row per (provider, event_id), whatever the deliveries: the first is
recorded with the event's allow-listed payload, each later one only counted.
"""

import asyncio
from datetime import timedelta

from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    PaymentEvent,
    PaymentProviderName,
)
from livecart_core.testing.backends import ReservationBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
EVENT = PaymentEvent(
    provider=PaymentProviderName.MOCK,
    event_id="evt_1",
    event_type=CheckoutEventType.COMPLETED.value,
    checkout_ref="mock_cs_1",
    payment_ref="mock_pi_1",
    payment_status=CheckoutPaymentStatus.PAID,
    amount_minor=2_500,
    currency="EUR",
    occurred_at=NOW - timedelta(seconds=3),
)


async def test_an_event_is_recorded_once_with_its_payload(backend: ReservationBackend) -> None:
    assert await backend.webhook_events.record(EVENT, now=NOW) is True

    registered = await backend.webhook_event(EVENT.provider, EVENT.event_id)
    assert registered is not None
    assert registered.event == EVENT
    assert registered.received_at == NOW
    assert (registered.delivery_count, registered.attempts) == (1, 0)
    assert (registered.processed_at, registered.outcome) == (None, None)


async def test_a_redelivery_is_counted_and_keeps_the_first_payload(
    backend: ReservationBackend,
) -> None:
    await backend.webhook_events.record(EVENT, now=NOW)
    # The same id with other fields: only the first delivery's payload counts.
    altered = EVENT.model_copy(update={"amount_minor": 1, "event_type": "altered"})

    again = [
        await backend.webhook_events.record(event, now=NOW + timedelta(seconds=n))
        for n, event in enumerate((EVENT, altered, EVENT, EVENT), start=1)
    ]

    registered = await backend.webhook_event(EVENT.provider, EVENT.event_id)
    assert again == [False] * 4
    assert registered is not None
    assert (registered.event, registered.received_at) == (EVENT, NOW)
    assert (registered.delivery_count, registered.attempts, registered.outcome) == (5, 0, None)


async def test_an_event_id_names_one_event_per_provider(backend: ReservationBackend) -> None:
    stripe = EVENT.model_copy(update={"provider": PaymentProviderName.STRIPE})

    assert await backend.webhook_events.record(EVENT, now=NOW) is True
    assert await backend.webhook_events.record(stripe, now=NOW) is True

    for provider in (PaymentProviderName.MOCK, PaymentProviderName.STRIPE):
        registered = await backend.webhook_event(provider, EVENT.event_id)
        assert registered is not None
        assert (registered.event.provider, registered.delivery_count) == (provider, 1)
    assert await backend.webhook_event(PaymentProviderName.MOCK, "evt_404") is None


async def test_deliveries_racing_each_other_end_on_one_row(backend: ReservationBackend) -> None:
    firsts = await asyncio.gather(
        *(backend.webhook_events.record(EVENT, now=NOW) for _ in range(10))
    )

    registered = await backend.webhook_event(EVENT.provider, EVENT.event_id)
    assert firsts.count(True) == 1
    assert registered is not None
    assert registered.delivery_count == 10
