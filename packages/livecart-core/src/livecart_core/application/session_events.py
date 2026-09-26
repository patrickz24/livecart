"""The session events that what happens to a comment or an order publishes.

Each builder turns a committed outcome into ``SessionEvent`` values, for the
``EventPublisher`` to append to the session's stream after the commit. The
dashboard upserts what it receives, by the id each event names, so an event
published twice (a redelivered comment republishes its stored outcome) is
harmless. ``data`` stays a plain JSON object until milestone 4b gives each
type its schema: identifiers, statuses and counters only, never the buyer's
platform identity, an address or a token.
"""

from datetime import datetime
from uuid import UUID

from livecart_core.application.dto import (
    OrderRecord,
    ReservationResult,
    SessionEvent,
    SessionEventType,
    StockLevel,
)
from livecart_core.domain.outcomes import RejectionReason


def intent_recorded(
    result: ReservationResult, *, correlation_id: str, occurred_at: datetime
) -> SessionEvent:
    """What unit 3 recorded for a comment: its intent and what became of it."""
    intent = result.intent
    return SessionEvent(
        type=SessionEventType.INTENT_RECORDED,
        session_id=intent.session_id,
        occurred_at=occurred_at,
        correlation_id=correlation_id,
        data={
            "intent_id": str(intent.id),
            "comment_id": str(intent.comment_id),
            "product_id": str(intent.product_id),
            "quantity": intent.quantity.value,
            "source": intent.source.value,
            "confidence": str(intent.confidence),
            "outcome": intent.outcome.value,
            "order_id": None if result.order is None else str(result.order.id),
        },
    )


def order_updated(
    order: OrderRecord, *, correlation_id: str | None, occurred_at: datetime
) -> SessionEvent:
    """An order as it stands: opened by a reservation, then paid or expired."""
    return SessionEvent(
        type=SessionEventType.ORDER_UPDATED,
        session_id=order.session_id,
        occurred_at=occurred_at,
        correlation_id=correlation_id,
        data={
            "order_id": str(order.id),
            "reference": order.reference.value,
            "status": order.status.value,
            "product_id": str(order.product_id),
            "quantity": order.quantity.value,
            "total_minor": order.total.amount_minor,
            "currency": order.total.currency,
            "reserved_until": order.reserved_until.isoformat(),
        },
    )


def stock_updated(
    session_id: UUID,
    product_id: UUID,
    stock: StockLevel,
    *,
    correlation_id: str | None,
    occurred_at: datetime,
) -> SessionEvent:
    """A product's counters after a change."""
    return SessionEvent(
        type=SessionEventType.STOCK_UPDATED,
        session_id=session_id,
        occurred_at=occurred_at,
        correlation_id=correlation_id,
        data={
            "product_id": str(product_id),
            "stock_total": stock.total,
            "reserved": stock.reserved,
            "sold": stock.sold,
            "available": stock.available,
        },
    )


def reservation_events(
    result: ReservationResult, *, correlation_id: str, occurred_at: datetime
) -> list[SessionEvent]:
    """Everything unit 3 changed: the intent, the order it opened if any, the product's stock."""
    events = [intent_recorded(result, correlation_id=correlation_id, occurred_at=occurred_at)]
    if result.order is not None:
        events.append(
            order_updated(result.order, correlation_id=correlation_id, occurred_at=occurred_at)
        )
    events.append(
        stock_updated(
            result.intent.session_id,
            result.intent.product_id,
            result.stock,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
        )
    )
    return events


def comment_rejected(
    session_id: UUID,
    comment_id: UUID,
    reason: RejectionReason,
    *,
    correlation_id: str,
    occurred_at: datetime,
) -> SessionEvent:
    """A comment of the session that carries no order, and why."""
    return SessionEvent(
        type=SessionEventType.COMMENT_REJECTED,
        session_id=session_id,
        occurred_at=occurred_at,
        correlation_id=correlation_id,
        data={"comment_id": str(comment_id), "reason": reason.value},
    )
