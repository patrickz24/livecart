"""``LiveSessionActivity.snapshot`` on every backend: the in-memory fake, then PostgreSQL.

The snapshot is what a dashboard starts from: the catalog with its stock,
and the newest intents (each with the order it opened), rejections and
orders of the session, never another session's, and the shipments of those
orders as they stand.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    CarrierName,
    IntentSource,
    ReservationRequest,
    ShipmentRef,
    StockLevel,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.outcomes import IntentOutcome, RejectionReason
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import LiveSessionStatus, ShipmentStatus
from livecart_core.testing.backends import ReservationBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START


async def _reserve(
    backend: ReservationBackend, session_id: UUID, comment: str, *, code: str = "B07", buyer: str
) -> None:
    comment_id = await backend.add_comment(comment, author=buyer)
    result = await backend.orders.reserve(
        ReservationRequest(
            comment_id=comment_id,
            session_id=session_id,
            product_code=ProductCode(code),
            variant=Variant(""),
            quantity=Quantity(1),
            source=IntentSource.PARSER,
            confidence=Decimal("1.00"),
            evidence=(code,),
            now=NOW,
            reserved_until=NOW + timedelta(seconds=900),
        )
    )
    assert result is not None


async def test_a_snapshot_holds_the_catalog_and_the_newest_activity_first(
    backend: ReservationBackend,
) -> None:
    session = await backend.add_live_session()
    b07 = await backend.add_product(session.id, "B07", stock_total=1)
    z99 = await backend.add_product(session.id, "Z99", stock_total=5)
    await _reserve(backend, session.id, "c-1", buyer="buyer-1")
    await _reserve(backend, session.id, "c-2", buyer="buyer-2")
    rejected = await backend.add_comment("c-3", author="buyer-3")
    await backend.comments.reject(rejected, RejectionReason.NO_INTENT, session_id=session.id)

    activity = await backend.activity.snapshot(session.id, limit=50)

    assert activity.session.id == session.id
    assert activity.session.status is LiveSessionStatus.LIVE
    assert [product.id for product in activity.products] == [b07.id, z99.id]
    assert activity.products[0].stock == StockLevel(total=1, reserved=1)
    # Newest first: the second comment found the stock gone.
    outcomes = [recorded.intent.outcome for recorded in activity.intents]
    assert outcomes == [IntentOutcome.OUT_OF_STOCK, IntentOutcome.RESERVED]
    [order] = activity.orders
    assert [recorded.order_id for recorded in activity.intents] == [None, order.id]
    assert order.intent_id == activity.intents[1].intent.id
    assert [rejection.comment_id for rejection in activity.rejections] == [rejected]
    assert activity.rejections[0].reason is RejectionReason.NO_INTENT


async def test_each_list_holds_at_most_the_limit_of_its_newest(
    backend: ReservationBackend,
) -> None:
    session = await backend.add_live_session()
    await backend.add_product(session.id, "B07", stock_total=10)
    for number in range(4):
        await _reserve(backend, session.id, f"c-{number}", buyer=f"buyer-{number}")
    for number in range(3):
        comment_id = await backend.add_comment(f"r-{number}")
        await backend.comments.reject(comment_id, RejectionReason.NO_INTENT, session_id=session.id)

    activity = await backend.activity.snapshot(session.id, limit=2)
    everything = await backend.activity.snapshot(session.id, limit=10)

    assert len(activity.intents) == len(activity.orders) == len(activity.rejections) == 2
    assert activity.intents == everything.intents[:2]
    assert activity.orders == everything.orders[:2]
    assert activity.rejections == everything.rejections[:2]
    assert [o.id for o in everything.orders] == sorted(
        (o.id for o in everything.orders), reverse=True
    )


async def test_a_snapshot_holds_the_shipments_of_its_orders_as_they_stand(
    backend: ReservationBackend,
) -> None:
    session = await backend.add_live_session()
    await backend.add_product(session.id, "B07", stock_total=10)
    for number in range(3):
        await _reserve(backend, session.id, f"c-{number}", buyer=f"buyer-{number}")
    newest, middle, oldest = (await backend.activity.snapshot(session.id, limit=10)).orders
    # The repository records the shipment of the order it is given: a paid one, in the worker.
    shipped = await backend.shipments.add(
        newest.id, ShipmentRef(CarrierName.MOCK, "mock_shp_1", "MOCK-0000000000000001", NOW)
    )
    moved = await backend.shipments.set_status(
        shipped.shipment.id, ShipmentStatus.IN_TRANSIT, event_at=NOW + timedelta(seconds=120)
    )
    await backend.shipments.add(
        oldest.id, ShipmentRef(CarrierName.MOCK, "mock_shp_2", "MOCK-0000000000000002", NOW)
    )

    activity = await backend.activity.snapshot(session.id, limit=2)

    assert [order.id for order in activity.orders] == [newest.id, middle.id]
    # Where the parcel is now, and only for the orders listed: the oldest is past the limit.
    assert activity.shipments == {newest.id: moved.shipment}
    assert moved.shipment.status is ShipmentStatus.IN_TRANSIT


async def test_a_snapshot_holds_its_own_session_only(backend: ReservationBackend) -> None:
    session = await backend.add_live_session()
    other = await backend.add_live_session(external_live_id="live-2")
    await backend.add_product(other.id, "B07", stock_total=10)
    await _reserve(backend, other.id, "c-1", buyer="buyer-1")
    comment_id = await backend.add_comment("r-1")
    await backend.comments.reject(comment_id, RejectionReason.NO_INTENT, session_id=other.id)

    activity = await backend.activity.snapshot(session.id, limit=10)

    assert activity.products == ()
    assert activity.intents == ()
    assert activity.orders == ()
    assert activity.shipments == {}
    assert activity.rejections == ()


async def test_an_unknown_session_has_no_snapshot(backend: ReservationBackend) -> None:
    with pytest.raises(RecordNotFound):
        await backend.activity.snapshot(UUID(int=404), limit=10)
