import secrets
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    IntentSource,
    ReservationRequest,
    StockLevel,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.reference import (
    RandomSource,
)
from livecart_core.domain.state_machines import (
    OrderStatus,
    TransitionOutcome,
)
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FAKE_CLOCK_START, InMemoryDatabase

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
LEASE = timedelta(seconds=30)
UNKNOWN = UUID(int=404)


async def _claimed(database: InMemoryDatabase, comment: str, *, author: str = "buyer-1") -> UUID:
    envelope = queued_comment(external_comment_id=comment, author=author)
    claim = await database.comments.claim(envelope, holder="worker-1", now=NOW, lease=LEASE)
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


# --- After the reservation (unit 3 itself runs on every backend: tests/contract) -----


async def _order_id(database: InMemoryDatabase, session_id: UUID, comment: str) -> UUID:
    author = f"buyer-of-{comment}"
    result = await database.orders.reserve(
        _request(await _claimed(database, comment, author=author), session_id)
    )
    assert result is not None
    assert result.order is not None
    return result.order.id


async def test_the_sweep_needs_an_aware_time() -> None:
    # Its limit and its rules run on every backend: tests/contract.
    database, _, _ = _database()

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
