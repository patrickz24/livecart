import secrets
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    IntentSource,
    PaymentProviderName,
    ReservationRequest,
    ShippingAddress,
    StockLevel,
)
from livecart_core.application.ports import RecordNotFound, ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.reference import (
    REFERENCE_SPACE,
    RandomSource,
    encode_reference,
)
from livecart_core.domain.state_machines import (
    CommentStatus,
    OrderStatus,
    TransitionOutcome,
)
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FAKE_CLOCK_START, InMemoryDatabase
from livecart_core.testing.randomness import SequenceRandomSource

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
LEASE = timedelta(seconds=30)
UNKNOWN = UUID(int=404)


async def _claimed(database: InMemoryDatabase, comment: str, *, author: str = "buyer-1") -> UUID:
    envelope = queued_comment(external_comment_id=comment, author=author)
    claim = await database.comments.claim(envelope, consumer="worker-1", now=NOW, lease=LEASE)
    return claim.comment.id


def _request(
    comment_id: UUID,
    session_id: UUID,
    code: str = "B07",
    *,
    variant: str = "",
    quantity: int = 1,
    ttl: timedelta = TTL,
) -> ReservationRequest:
    return ReservationRequest(
        comment_id=comment_id,
        session_id=session_id,
        product_code=ProductCode(code),
        variant=Variant(variant),
        quantity=Quantity(quantity),
        source=IntentSource.PARSER,
        confidence=Decimal("1.00"),
        evidence=("je prends", code),
        now=NOW,
        reserved_until=NOW + ttl,
    )


def _database(
    *, stock_total: int = 10, random_source: RandomSource = secrets
) -> tuple[InMemoryDatabase, UUID, UUID]:
    """A live session selling B07 (no variant) and A12 M: the database, the session, B07."""
    database = InMemoryDatabase(random_source=random_source)
    session = database.add_live_session()
    b07 = database.add_product(
        session.id, "B07", price=Money(1_250, "EUR"), stock_total=stock_total
    )
    database.add_product(session.id, "A12", variant="M", stock_total=stock_total)
    return database, session.id, b07.id


# --- Unit 3: reference draws (the other scenarios run on every backend, tests/contract) ---


async def test_a_colliding_reference_is_drawn_again() -> None:
    source = SequenceRandomSource([5, 5, 6])
    database, session_id, _ = _database(random_source=source)

    first = await database.orders.reserve(
        _request(await _claimed(database, "comment-1"), session_id)
    )
    second = await database.orders.reserve(
        _request(await _claimed(database, "comment-2", author="buyer-2"), session_id)
    )

    assert first is not None
    assert second is not None
    assert first.order is not None
    assert second.order is not None
    assert first.order.reference == encode_reference(5)
    assert second.order.reference == encode_reference(6)
    assert source.requested_bounds == [REFERENCE_SPACE] * 3


async def test_three_collisions_leave_everything_as_it_was() -> None:
    database, session_id, product_id = _database(random_source=SequenceRandomSource([5, 5, 5, 5]))
    await database.orders.reserve(_request(await _claimed(database, "comment-1"), session_id))
    comment_id = await _claimed(database, "comment-2", author="buyer-2")

    with pytest.raises(ServiceUnavailable, match="collided"):
        await database.orders.reserve(_request(comment_id, session_id))

    comment = await database.comments.get(comment_id)
    assert comment is not None
    assert comment.status is CommentStatus.RECEIVED
    assert await database.orders.find_reservation(comment_id) is None
    assert len(database.tables.intents) == 1
    product = await database.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10, reserved=1)


# --- After the reservation -------------------------------------------------------------


async def _order_id(database: InMemoryDatabase, session_id: UUID, comment: str) -> UUID:
    author = f"buyer-of-{comment}"
    result = await database.orders.reserve(
        _request(await _claimed(database, comment, author=author), session_id)
    )
    assert result is not None
    assert result.order is not None
    return result.order.id


async def test_the_buyer_is_marked_notified_once() -> None:
    database, session_id, _ = _database()
    order_id = await _order_id(database, session_id, "comment-1")

    first = await database.orders.mark_buyer_notified(order_id, now=NOW)
    again = await database.orders.mark_buyer_notified(order_id, now=NOW + timedelta(seconds=5))

    assert first.buyer_notified_at == NOW
    assert again.buyer_notified_at == NOW
    with pytest.raises(RecordNotFound):
        await database.orders.mark_buyer_notified(UNKNOWN, now=NOW)


async def test_an_order_records_its_shipping_address() -> None:
    database, session_id, _ = _database()
    order_id = await _order_id(database, session_id, "comment-1")
    address = ShippingAddress(
        full_name="Camille Martin",
        line1="12 rue des Lilas",
        postal_code="69003",
        city="Lyon",
        country="FR",
    )

    order = await database.orders.set_shipping_address(order_id, address)

    assert order.shipping_address == address
    assert await database.orders.get(order_id) == order
    with pytest.raises(RecordNotFound):
        await database.orders.set_shipping_address(UNKNOWN, address)


async def test_the_sweep_expires_due_orders_without_pending_payment() -> None:
    database, session_id, product_id = _database()
    due = await _order_id(database, session_id, "comment-1")
    paying = await _order_id(database, session_id, "comment-2")
    await database.payments.open_attempt(
        paying,
        provider=PaymentProviderName.MOCK,
        return_url="livecart://payment-return",
        now=NOW,
        grace=timedelta(seconds=60),
        max_attempts=3,
    )
    later = await database.orders.reserve(
        _request(await _claimed(database, "comment-3", author="late"), session_id, ttl=2 * TTL)
    )
    assert later is not None
    assert later.order is not None
    sweep_at = NOW + TTL + timedelta(seconds=1)

    expired = await database.orders.expire_due(now=sweep_at, limit=10)

    assert [order.id for order in expired] == [due]
    assert expired[0].status is OrderStatus.EXPIRED
    assert expired[0].expired_at == sweep_at
    assert await database.orders.get(due) == expired[0]
    product = await database.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10, reserved=2)
    assert await database.orders.expire_due(now=sweep_at, limit=10) == []


async def test_a_reservation_ending_exactly_now_is_not_due_yet() -> None:
    database, session_id, _ = _database()
    await _order_id(database, session_id, "comment-1")

    assert await database.orders.expire_due(now=NOW + TTL, limit=10) == []


async def test_the_sweep_takes_the_lowest_ids_first_within_its_limit() -> None:
    database, session_id, _ = _database()
    ids = [await _order_id(database, session_id, f"comment-{n}") for n in range(3)]

    first = await database.orders.expire_due(now=NOW + 2 * TTL, limit=2)
    rest = await database.orders.expire_due(now=NOW + 2 * TTL, limit=2)

    assert [order.id for order in first] == ids[:2]
    assert [order.id for order in rest] == ids[2:]


async def test_the_sweep_refuses_nonsensical_arguments() -> None:
    database, _, _ = _database()

    with pytest.raises(ValueError, match="limit"):
        await database.orders.expire_due(now=NOW, limit=0)
    with pytest.raises(ValueError, match="naive"):
        await database.orders.expire_due(now=NOW.replace(tzinfo=None), limit=1)


async def test_a_payment_sells_the_reserved_units_once() -> None:
    database, session_id, product_id = _database()
    order_id = await _order_id(database, session_id, "comment-1")
    paid_at = NOW + timedelta(minutes=3)

    paid = await database.orders.record_payment(order_id, now=paid_at)
    replayed = await database.orders.record_payment(order_id, now=paid_at + timedelta(minutes=1))

    assert paid.outcome is TransitionOutcome.APPLIED
    assert (paid.order.status, paid.order.paid_at) == (OrderStatus.PAID, paid_at)
    assert replayed.outcome is TransitionOutcome.NOOP
    assert replayed.order == paid.order
    product = await database.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10, sold=1)


async def test_a_payment_after_expiry_has_no_stock_effect() -> None:
    database, session_id, product_id = _database()
    order_id = await _order_id(database, session_id, "comment-1")
    await database.orders.expire_due(now=NOW + 2 * TTL, limit=10)

    late = await database.orders.record_payment(order_id, now=NOW + 3 * TTL)
    replayed = await database.orders.record_payment(order_id, now=NOW + 4 * TTL)

    assert late.outcome is TransitionOutcome.APPLIED
    assert late.order.status is OrderStatus.PAID_AFTER_EXPIRY
    assert late.order.paid_at == NOW + 3 * TTL
    assert replayed.outcome is TransitionOutcome.NOOP
    product = await database.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10)
    with pytest.raises(RecordNotFound):
        await database.orders.record_payment(UNKNOWN, now=NOW)
