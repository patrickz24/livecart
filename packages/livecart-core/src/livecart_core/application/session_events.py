"""The session events that what happens to a comment or an order publishes, and their payloads.

Each builder turns a committed outcome into ``SessionEvent`` values, for the
``EventPublisher`` to append to the session's stream after the commit. The
dashboard upserts what it receives, by the id each payload names, so an event
published twice (a redelivered comment republishes its stored outcome) is
harmless.

Each event type has its payload model, which the builders fill and the SSE
endpoint's snapshot reuses: a dashboard reads the same record from a
snapshot's lists as from an event. The payloads hold identifiers, statuses,
amounts and counters only, never the buyer's platform identity, an address
or a token.
"""

from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from livecart_core.application.dto import (
    CarrierName,
    IntentSource,
    LiveSessionRecord,
    OrderRecord,
    PurchaseIntentRecord,
    ReservationResult,
    SessionEvent,
    SessionEventType,
    ShipmentRecord,
    StockLevel,
)
from livecart_core.domain.outcomes import IntentOutcome, RejectionReason
from livecart_core.domain.quantity import QUANTITY_CEILING
from livecart_core.domain.reference import (
    CROCKFORD_ALPHABET,
    REFERENCE_PREFIX,
    REFERENCE_SYMBOLS,
)
from livecart_core.domain.state_machines import LiveSessionStatus, OrderStatus, ShipmentStatus

_PAYLOAD = ConfigDict(extra="forbid", frozen=True)


class IntentPayload(BaseModel):
    """``intent.recorded``: a purchase intent, and what its reservation made of it."""

    model_config = _PAYLOAD

    intent_id: UUID
    comment_id: UUID
    product_id: UUID
    quantity: int = Field(ge=1, le=QUANTITY_CEILING)
    source: IntentSource
    confidence: Decimal = Field(ge=0, le=1)
    outcome: IntentOutcome
    order_id: UUID | None = Field(description="The order the reservation opened, if any.")

    @classmethod
    def of(cls, intent: PurchaseIntentRecord, order_id: UUID | None) -> Self:
        return cls(
            intent_id=intent.id,
            comment_id=intent.comment_id,
            product_id=intent.product_id,
            quantity=intent.quantity.value,
            source=intent.source,
            confidence=intent.confidence,
            outcome=intent.outcome,
            order_id=order_id,
        )


class RejectionPayload(BaseModel):
    """``comment.rejected``: a comment of the session that carries no intent, and why."""

    model_config = _PAYLOAD

    comment_id: UUID
    reason: RejectionReason


class OrderPayload(BaseModel):
    """``order.updated``: an order as it stands, opened by a reservation, then paid or expired."""

    model_config = _PAYLOAD

    order_id: UUID
    reference: str = Field(
        pattern=rf"^{REFERENCE_PREFIX}[{CROCKFORD_ALPHABET}]{{{REFERENCE_SYMBOLS}}}$"
    )
    status: OrderStatus
    product_id: UUID
    quantity: int = Field(ge=1, le=QUANTITY_CEILING)
    total_minor: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reserved_until: AwareDatetime
    paid_at: AwareDatetime | None = None
    expired_at: AwareDatetime | None = None

    @classmethod
    def of(cls, order: OrderRecord) -> Self:
        return cls(
            order_id=order.id,
            reference=order.reference.value,
            status=order.status,
            product_id=order.product_id,
            quantity=order.quantity.value,
            total_minor=order.total.amount_minor,
            currency=order.total.currency,
            reserved_until=order.reserved_until,
            paid_at=order.paid_at,
            expired_at=order.expired_at,
        )


class StockPayload(BaseModel):
    """``stock.updated``: a product's counters after a change."""

    model_config = _PAYLOAD

    product_id: UUID
    stock_total: int = Field(ge=0)
    reserved: int = Field(ge=0)
    sold: int = Field(ge=0)
    available: int = Field(ge=0)

    @classmethod
    def of(cls, product_id: UUID, stock: StockLevel) -> Self:
        return cls(
            product_id=product_id,
            stock_total=stock.total,
            reserved=stock.reserved,
            sold=stock.sold,
            available=stock.available,
        )


class ShipmentPayload(BaseModel):
    """``shipment.updated``: a paid order's shipment, as its carrier reports it (milestone 6)."""

    model_config = _PAYLOAD

    shipment_id: UUID
    order_id: UUID
    status: ShipmentStatus
    carrier: CarrierName
    tracking_number: str = Field(min_length=1, max_length=64)
    last_event_at: AwareDatetime | None = None

    @classmethod
    def of(cls, shipment: ShipmentRecord) -> Self:
        return cls(
            shipment_id=shipment.id,
            order_id=shipment.order_id,
            status=shipment.status,
            carrier=shipment.ref.carrier,
            tracking_number=shipment.ref.tracking_number,
            last_event_at=shipment.last_event_at,
        )


class SessionPayload(BaseModel):
    """``session.updated``: the session started or ended; an ended session reserves no more."""

    model_config = _PAYLOAD

    status: LiveSessionStatus
    title: str
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None

    @classmethod
    def of(cls, session: LiveSessionRecord) -> Self:
        return cls(
            status=session.status,
            title=session.title,
            started_at=session.started_at,
            ended_at=session.ended_at,
        )


def _event(
    event_type: SessionEventType,
    session_id: UUID,
    payload: BaseModel,
    *,
    correlation_id: str | None,
    occurred_at: datetime,
) -> SessionEvent:
    return SessionEvent(
        type=event_type,
        session_id=session_id,
        occurred_at=occurred_at,
        correlation_id=correlation_id,
        data=payload.model_dump(mode="json"),
    )


def intent_recorded(
    result: ReservationResult, *, correlation_id: str, occurred_at: datetime
) -> SessionEvent:
    """What unit 3 recorded for a comment: its intent and what became of it."""
    order_id = None if result.order is None else result.order.id
    return _event(
        SessionEventType.INTENT_RECORDED,
        result.intent.session_id,
        IntentPayload.of(result.intent, order_id),
        correlation_id=correlation_id,
        occurred_at=occurred_at,
    )


def order_updated(
    order: OrderRecord, *, correlation_id: str | None, occurred_at: datetime
) -> SessionEvent:
    """An order as it stands: opened by a reservation, then paid or expired."""
    return _event(
        SessionEventType.ORDER_UPDATED,
        order.session_id,
        OrderPayload.of(order),
        correlation_id=correlation_id,
        occurred_at=occurred_at,
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
    return _event(
        SessionEventType.STOCK_UPDATED,
        session_id,
        StockPayload.of(product_id, stock),
        correlation_id=correlation_id,
        occurred_at=occurred_at,
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


def session_updated(session: LiveSessionRecord, *, occurred_at: datetime) -> SessionEvent:
    """A session that started or ended, as it stands: no comment caused it, so no correlation."""
    return _event(
        SessionEventType.SESSION_UPDATED,
        session.id,
        SessionPayload.of(session),
        correlation_id=None,
        occurred_at=occurred_at,
    )


def shipment_updated(
    session_id: UUID,
    shipment: ShipmentRecord,
    *,
    correlation_id: str | None,
    occurred_at: datetime,
) -> SessionEvent:
    """A shipment of an order of the session, as its carrier reports it."""
    return _event(
        SessionEventType.SHIPMENT_UPDATED,
        session_id,
        ShipmentPayload.of(shipment),
        correlation_id=correlation_id,
        occurred_at=occurred_at,
    )


def comment_rejected(
    session_id: UUID,
    comment_id: UUID,
    reason: RejectionReason,
    *,
    correlation_id: str,
    occurred_at: datetime,
) -> SessionEvent:
    """A comment of the session that carries no order, and why."""
    return _event(
        SessionEventType.COMMENT_REJECTED,
        session_id,
        RejectionPayload(comment_id=comment_id, reason=reason),
        correlation_id=correlation_id,
        occurred_at=occurred_at,
    )
