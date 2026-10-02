"""The ``LiveSessionEvent`` union in the OpenAPI document and in the generated TypeScript client.

FastAPI documents a stream of ``ServerSentEvent`` as generic SSE items, so
``LiveCartApi.openapi`` adds the stream's models to ``components.schemas``:
these tests prove they are there, discriminated by every ``type`` literal,
that the stream's item points at them, that the committed client exports the
union, and that every event the application builds fits it.
"""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from livecart_api.routes.session_events import (
    CLOSE_EVENT,
    GAP_EVENT,
    SNAPSHOT_EVENT,
    LiveSessionEvent,
    add_components,
)
from livecart_api.testing import make_app
from livecart_core.application.dto import (
    CarrierName,
    IntentSource,
    OrderRecord,
    PurchaseIntentRecord,
    ReservationResult,
    SessionEvent,
    SessionEventType,
    ShipmentRecord,
    ShipmentRef,
    StockLevel,
)
from livecart_core.application.session_events import (
    comment_rejected,
    reservation_events,
    session_updated,
    shipment_updated,
)
from livecart_core.domain.money import Money
from livecart_core.domain.outcomes import IntentOutcome, RejectionReason
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.reference import OrderReference
from livecart_core.domain.state_machines import OrderStatus, ShipmentStatus
from livecart_core.testing.fakes import FAKE_CLOCK_START, InMemoryDatabase

REPO_ROOT = Path(__file__).resolve().parents[4]
CLIENT = REPO_ROOT / "packages" / "api-client"
EVENT_TYPES = {
    "intent.recorded",
    "comment.rejected",
    "order.updated",
    "stock.updated",
    "shipment.updated",
    "session.updated",
    "stream.gap",
    "stream.close",
}
EVENTS_PATH = "/api/v1/live-sessions/{session_id}/events"
REF = "#/components/schemas/"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    document: dict[str, Any] = make_app().openapi()
    return document


def _component(schema: dict[str, Any], ref: str) -> dict[str, Any]:
    assert ref.startswith(REF)
    component: dict[str, Any] = schema["components"]["schemas"][ref.removeprefix(REF)]
    return component


def test_the_union_is_a_component_discriminated_by_every_type(schema: dict[str, Any]) -> None:
    union = schema["components"]["schemas"]["LiveSessionEvent"]

    assert union["discriminator"]["propertyName"] == "type"
    assert set(union["discriminator"]["mapping"]) == EVENT_TYPES
    members = [_component(schema, member["$ref"]) for member in union["oneOf"]]
    literals = {member["properties"]["type"]["const"] for member in members}
    assert literals == EVENT_TYPES
    for member in members:
        assert {"type", "session_id", "occurred_at", "correlation_id", "data"} <= set(
            member["required"]
        )
        assert _component(schema, member["properties"]["data"]["$ref"])


def test_the_stream_item_holds_a_snapshot_or_an_event(schema: dict[str, Any]) -> None:
    response = schema["paths"][EVENTS_PATH]["get"]["responses"]["200"]
    item = response["content"]["text/event-stream"]["itemSchema"]

    content = item["properties"]["data"]["contentSchema"]["oneOf"]
    assert [ref["$ref"] for ref in content] == [
        f"{REF}LiveSessionSnapshot",
        f"{REF}LiveSessionEvent",
    ]
    assert set(item["properties"]["event"]["enum"]) == EVENT_TYPES | {SNAPSHOT_EVENT}
    snapshot = _component(schema, content[0]["$ref"])
    assert {"session", "events_cursor", "products", "intents", "rejections", "orders"} <= set(
        snapshot["required"]
    )
    # Each order carries its shipment, the payload of shipment.updated, or null: always present.
    order = _component(schema, snapshot["properties"]["orders"]["items"]["$ref"])
    assert "shipment" in order["required"]
    assert order["properties"]["shipment"]["anyOf"] == [
        {"$ref": f"{REF}ShipmentPayload"},
        {"type": "null"},
    ]


def test_every_reference_of_the_document_resolves(schema: dict[str, Any]) -> None:
    text = json.dumps(schema)
    references = {part.split('"')[0] for part in text.split(f'"$ref": "{REF}')[1:]}

    assert references <= set(schema["components"]["schemas"])


def test_the_committed_contract_holds_the_union() -> None:
    committed = json.loads((CLIENT / "openapi.json").read_text(encoding="utf-8"))

    assert "LiveSessionEvent" in committed["components"]["schemas"]
    assert "LiveSessionSnapshot" in committed["components"]["schemas"]


def test_the_generated_typescript_client_exports_the_union() -> None:
    types = (CLIENT / "src" / "generated" / "types.gen.ts").read_text(encoding="utf-8")
    index = (CLIENT / "src" / "generated" / "index.ts").read_text(encoding="utf-8")

    union = types.split("export type LiveSessionEvent = ", 1)[1].split(";\n\n", 1)[0]
    for event_type in EVENT_TYPES:
        assert f"type: '{event_type}';" in union
    assert "export type LiveSessionSnapshot = {" in types
    snapshot_order = types.split("export type SnapshotOrder = {", 1)[1].split("\n};", 1)[0]
    assert "shipment: ShipmentPayload | null;" in snapshot_order
    assert "LiveSessionEvent," in index
    assert "LiveSessionSnapshot," in index


def test_a_component_generated_twice_differently_is_refused() -> None:
    schema: dict[str, Any] = {"components": {"schemas": {"StockPayload": {"type": "null"}}}}

    with pytest.raises(RuntimeError, match="two definitions of the OpenAPI component"):
        add_components(schema, (LiveSessionEvent,))


# --- Every event the application publishes fits the union ------------------------------

SESSION = UUID(int=1)
NOW = FAKE_CLOCK_START


def _reservation() -> ReservationResult:
    intent = PurchaseIntentRecord(
        id=UUID(int=10),
        comment_id=UUID(int=11),
        session_id=SESSION,
        product_id=UUID(int=12),
        quantity=Quantity(2),
        source=IntentSource.PARSER,
        confidence=Decimal("0.90"),
        evidence=("B07",),
        outcome=IntentOutcome.RESERVED,
    )
    order = OrderRecord(
        id=UUID(int=13),
        reference=OrderReference("LC-7K3F9Q"),
        seller_id=UUID(int=14),
        session_id=SESSION,
        intent_id=intent.id,
        product_id=intent.product_id,
        quantity=Quantity(2),
        unit_price=Money(1_250, "EUR"),
        total=Money(2_500, "EUR"),
        status=OrderStatus.PENDING_PAYMENT,
        reserved_until=NOW + timedelta(seconds=900),
        buyer_platform_user_id="buyer-1",
        created_at=NOW,
    )
    return ReservationResult(intent, order, StockLevel(total=5, reserved=2))


def _published() -> list[SessionEvent]:
    session = InMemoryDatabase().add_live_session()
    shipment = ShipmentRecord(
        id=UUID(int=20),
        order_id=UUID(int=13),
        ref=ShipmentRef(CarrierName.MOCK, "mock-1", "MOCK123456", NOW),
        status=ShipmentStatus.IN_TRANSIT,
        last_event_at=NOW,
    )
    return [
        *reservation_events(_reservation(), correlation_id="correlation-1", occurred_at=NOW),
        comment_rejected(
            SESSION, UUID(int=15), RejectionReason.NO_INTENT, correlation_id="c", occurred_at=NOW
        ),
        session_updated(session, occurred_at=NOW),
        shipment_updated(SESSION, shipment, correlation_id=None, occurred_at=NOW),
    ]


def test_every_event_the_application_builds_fits_the_union() -> None:
    events = _published()

    relayed = [LiveSessionEvent.model_validate(event.model_dump(mode="json")) for event in events]

    assert {event.type for event in events} == set(SessionEventType)
    for event, message in zip(events, relayed, strict=True):
        assert message.root.type == event.type.value
        assert message.model_dump(mode="json") == event.model_dump(mode="json")


def test_the_stream_messages_fit_the_union_too() -> None:
    for message in (
        {"type": GAP_EVENT, "data": {"after": "1-0"}},
        {"type": CLOSE_EVENT, "data": {"reason": "max_duration"}},
    ):
        parsed = LiveSessionEvent.model_validate(
            {**message, "session_id": str(SESSION), "occurred_at": NOW.isoformat()}
        )
        assert parsed.root.type == message["type"]


def test_a_payload_that_does_not_fit_its_type_is_refused() -> None:
    event = _published()[2].model_dump(mode="json")  # stock.updated

    with pytest.raises(ValidationError):
        LiveSessionEvent.model_validate({**event, "type": "order.updated"})
