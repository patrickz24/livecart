import asyncio
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
from livecart_core.domain.outcomes import IntentOutcome, RejectionReason
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.reference import (
    REFERENCE_SPACE,
    OrderReference,
    RandomSource,
    encode_reference,
)
from livecart_core.domain.state_machines import (
    CommentStatus,
    LiveSessionStatus,
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


# --- Unit 3 ----------------------------------------------------------------------------


async def test_a_reservation_opens_a_pending_order_for_the_author() -> None:
    database, session_id, product_id = _database()
    comment_id = await _claimed(database, "comment-1", author="buyer-7")

    result = await database.orders.reserve(_request(comment_id, session_id, quantity=2))

    assert result is not None
    order = result.order
    assert result.intent.outcome is IntentOutcome.RESERVED
    assert result.intent.product_id == product_id
    assert result.intent.evidence == ("je prends", "B07")
    assert order is not None
    assert order.status is OrderStatus.PENDING_PAYMENT
    assert order.intent_id == result.intent.id
    assert order.buyer_platform_user_id == "buyer-7"
    assert order.seller_id == database.tables.sessions[session_id].seller_id
    assert (order.unit_price, order.total) == (Money(1_250, "EUR"), Money(2_500, "EUR"))
    assert order.reserved_until == NOW + TTL
    assert order.created_at == NOW
    assert result.stock == StockLevel(10, reserved=2)
    product = await database.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10, reserved=2)
    comment = await database.comments.get(comment_id)
    assert comment is not None
    assert (comment.status, comment.session_id) == (CommentStatus.PROCESSED, session_id)
    assert await database.orders.find_reservation(comment_id) == result
    assert await database.orders.get(order.id) == order
    assert await database.orders.get_by_reference(order.reference) == order


async def test_short_stock_records_an_out_of_stock_intent() -> None:
    database, session_id, _ = _database(stock_total=1)
    comment_id = await _claimed(database, "comment-1")

    result = await database.orders.reserve(_request(comment_id, session_id, quantity=2))

    assert result is not None
    assert result.intent.outcome is IntentOutcome.OUT_OF_STOCK
    assert result.order is None
    assert result.stock == StockLevel(1)
    assert database.tables.orders == {}
    comment = await database.comments.get(comment_id)
    assert comment is not None
    assert comment.status is CommentStatus.PROCESSED


async def test_a_session_no_longer_live_reserves_nothing() -> None:
    database, session_id, _ = _database()
    comment_id = await _claimed(database, "comment-1")
    await database.live_sessions.set_status(session_id, LiveSessionStatus.ENDED, now=NOW)

    result = await database.orders.reserve(_request(comment_id, session_id))

    assert result is not None
    assert result.intent.outcome is IntentOutcome.OUT_OF_STOCK
    assert result.stock == StockLevel(10)


async def test_one_pending_order_per_buyer_and_product() -> None:
    database, session_id, _ = _database()
    first = await database.orders.reserve(
        _request(await _claimed(database, "comment-1"), session_id)
    )
    same = await database.orders.reserve(
        _request(await _claimed(database, "comment-2"), session_id)
    )
    other_product = await database.orders.reserve(
        _request(await _claimed(database, "comment-3"), session_id, "A12", variant="M")
    )
    other_buyer = await database.orders.reserve(
        _request(await _claimed(database, "comment-4", author="buyer-2"), session_id)
    )

    assert first is not None
    assert same is not None
    assert other_product is not None
    assert other_buyer is not None
    assert same.intent.outcome is IntentOutcome.ALREADY_RESERVED
    assert same.order is None
    assert same.stock == StockLevel(10, reserved=1)
    assert other_product.intent.outcome is IntentOutcome.RESERVED
    assert other_buyer.intent.outcome is IntentOutcome.RESERVED
    assert other_buyer.stock == StockLevel(10, reserved=2)


async def test_an_expired_order_frees_the_buyer_for_a_new_one() -> None:
    database, session_id, _ = _database()
    await database.orders.reserve(_request(await _claimed(database, "comment-1"), session_id))
    await database.orders.expire_due(now=NOW + TTL + timedelta(seconds=1), limit=10)

    again = await database.orders.reserve(
        _request(await _claimed(database, "comment-2"), session_id)
    )

    assert again is not None
    assert again.intent.outcome is IntentOutcome.RESERVED


async def test_a_settled_comment_reserves_nothing_and_writes_nothing() -> None:
    database, session_id, _ = _database()
    processed = await _claimed(database, "comment-1")
    rejected = await _claimed(database, "comment-2")
    await database.orders.reserve(_request(processed, session_id))
    await database.comments.reject(rejected, RejectionReason.NO_INTENT, session_id=session_id)
    intents, orders = dict(database.tables.intents), dict(database.tables.orders)

    assert await database.orders.reserve(_request(processed, session_id)) is None
    assert await database.orders.reserve(_request(rejected, session_id)) is None
    assert (database.tables.intents, database.tables.orders) == (intents, orders)


async def test_twenty_parallel_buyers_never_oversell() -> None:
    database, session_id, product_id = _database(stock_total=5)
    comments = [await _claimed(database, f"comment-{n}", author=f"buyer-{n}") for n in range(20)]

    results = await asyncio.gather(
        *(database.orders.reserve(_request(comment_id, session_id)) for comment_id in comments)
    )

    outcomes = [result.intent.outcome for result in results if result is not None]
    assert outcomes.count(IntentOutcome.RESERVED) == 5
    assert outcomes.count(IntentOutcome.OUT_OF_STOCK) == 15
    product = await database.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(5, reserved=5)


async def test_twenty_parallel_comments_of_one_buyer_give_one_order() -> None:
    database, session_id, _ = _database()
    comments = [await _claimed(database, f"comment-{n}") for n in range(20)]

    results = await asyncio.gather(
        *(database.orders.reserve(_request(comment_id, session_id)) for comment_id in comments)
    )

    outcomes = [result.intent.outcome for result in results if result is not None]
    assert outcomes.count(IntentOutcome.RESERVED) == 1
    assert outcomes.count(IntentOutcome.ALREADY_RESERVED) == 19
    assert len(database.tables.orders) == 1


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


async def test_unit_3_needs_a_known_comment_session_and_product() -> None:
    database, session_id, _ = _database()
    comment_id = await _claimed(database, "comment-1")

    with pytest.raises(RecordNotFound, match="comment"):
        await database.orders.reserve(_request(UNKNOWN, session_id))
    with pytest.raises(RecordNotFound, match="live session"):
        await database.orders.reserve(_request(comment_id, UNKNOWN))
    with pytest.raises(RecordNotFound, match="product"):
        await database.orders.reserve(_request(comment_id, session_id, "Z99"))
    comment = await database.comments.get(comment_id)
    assert comment is not None
    assert comment.status is CommentStatus.RECEIVED


async def test_lookups_of_what_does_not_exist() -> None:
    database, _, _ = _database()

    assert await database.orders.find_reservation(UNKNOWN) is None
    assert await database.orders.get(UNKNOWN) is None
    assert await database.orders.get_by_reference(OrderReference("LC-000000")) is None


async def test_an_out_of_stock_reservation_reads_back_without_order() -> None:
    database, session_id, _ = _database(stock_total=0)
    comment_id = await _claimed(database, "comment-1")
    await database.orders.reserve(_request(comment_id, session_id))

    stored = await database.orders.find_reservation(comment_id)

    assert stored is not None
    assert stored.intent.outcome is IntentOutcome.OUT_OF_STOCK
    assert stored.order is None


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
