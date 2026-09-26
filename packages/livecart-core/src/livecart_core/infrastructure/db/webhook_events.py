"""The registry of payment webhooks, ``processed_webhook_events``, on PostgreSQL.

``record`` is what the payment webhook writes once the provider's signature
holds, and all it writes: one statement, in a transaction of its own, of
SQLAlchemy Core constructs::

    INSERT INTO processed_webhook_events (id, provider, event_id, event_type, payload, ...)
    VALUES (...)
    ON CONFLICT (provider, event_id)
    DO UPDATE SET delivery_count = processed_webhook_events.delivery_count + 1
    RETURNING delivery_count

The unique constraint ``uq_processed_webhook_events_provider_event_id`` makes
the registry the durable idempotence of the payment webhooks: a provider's
redelivery, or two deliveries of one event racing each other, end on the one
row, which counts them. ``delivery_count`` is 1 on the row this statement
inserted and more on a redelivery, which keeps the first payload, type and
processing: the worker applies an event once, whatever the deliveries. The
payload is ``PaymentEvent``, the allow-list the provider's adapter reduced the
event to, never the provider's body.
"""

from datetime import datetime
from typing import Final

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import PaymentEvent
from livecart_core.infrastructure.db.ids import new_id
from livecart_core.infrastructure.db.tables import processed_webhook_events
from livecart_core.infrastructure.db.units import transaction

EVENT_KEY: Final = "uq_processed_webhook_events_provider_event_id"


class SqlWebhookEventRegistry:
    """The registry of payment webhooks; see the module docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, event: PaymentEvent, *, now: datetime) -> bool:
        table = processed_webhook_events
        statement = (
            insert(table)
            .values(
                id=new_id(),
                provider=event.provider.value,
                event_id=event.event_id,
                event_type=event.event_type,
                payload=event.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint=EVENT_KEY,
                # ON CONFLICT runs no Python-side onupdate: updated_at is set here.
                set_={"delivery_count": table.c.delivery_count + 1, "updated_at": now},
            )
            .returning(table.c.delivery_count)
        )
        async with transaction(self._engine) as connection:
            delivery_count = (await connection.execute(statement)).scalar_one()
        return bool(delivery_count == 1)
