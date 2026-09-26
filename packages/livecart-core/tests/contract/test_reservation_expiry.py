"""``ReservationExpiry.expire_due`` on every backend: the in-memory fake, then PostgreSQL.

The first step of the expiry sweep: the pending orders whose reservation
ended, without a payment in flight, expired in batches and their stock
released, each exactly once however many sweeps run.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    ExpiredReservations,
    IntentSource,
    OrderRecord,
    ReservationRequest,
    StockLevel,
)
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus
from livecart_core.testing.backends import ReservationBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
DUE = NOW + TTL + timedelta(seconds=1)


async def _seed(backend: ReservationBackend) -> tuple[UUID, UUID, UUID]:
    """A live session selling B07 and A12 M, ten of each: the session, B07, A12 M."""
    session = await backend.add_live_session()
    b07 = await backend.add_product(session.id, "B07", stock_total=10)
    a12 = await backend.add_product(session.id, "A12", variant="M", stock_total=10)
    return session.id, b07.id, a12.id


async def _open(
    backend: ReservationBackend,
    session_id: UUID,
    comment: str,
    *,
    code: str = "B07",
    variant: str = "",
    quantity: int = 1,
    ttl: timedelta = TTL,
) -> OrderRecord:
    """A pending order for a buyer of its own, reserved until ``NOW + ttl``."""
    comment_id = await backend.add_comment(comment, author=f"buyer-of-{comment}")
    result = await backend.orders.reserve(
        ReservationRequest(
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
    )
    assert result is not None
    assert result.order is not None
    return result.order


async def _stock(backend: ReservationBackend, product_id: UUID) -> StockLevel:
    product = await backend.products.get(product_id)
    assert product is not None
    return product.stock


async def test_the_sweep_expires_due_orders_and_releases_their_stock(
    backend: ReservationBackend,
) -> None:
    session_id, b07, a12 = await _seed(backend)
    two = await _open(backend, session_id, "c-1", quantity=2)
    three = await _open(backend, session_id, "c-2", quantity=3)
    a12_order = await _open(backend, session_id, "c-3", code="A12", variant="M")
    later = await _open(backend, session_id, "c-4", ttl=2 * TTL)

    expired = await backend.orders.expire_due(now=DUE, limit=10)

    assert [order.id for order in expired.orders] == [two.id, three.id, a12_order.id]
    assert {order.status for order in expired.orders} == {OrderStatus.EXPIRED}
    assert {order.expired_at for order in expired.orders} == {DUE}
    # One release per product, of all its orders at once.
    assert expired.stock == {b07: StockLevel(10, reserved=1), a12: StockLevel(10)}
    assert await _stock(backend, b07) == StockLevel(10, reserved=1)
    assert await _stock(backend, a12) == StockLevel(10)
    assert await backend.orders.get(two.id) == expired.orders[0]
    still = await backend.orders.get(later.id)
    assert still is not None
    assert still.status is OrderStatus.PENDING_PAYMENT
    assert await backend.orders.expire_due(now=DUE, limit=10) == ExpiredReservations()


async def test_an_order_with_a_payment_in_flight_is_left_to_the_payment(
    backend: ReservationBackend,
) -> None:
    session_id, b07, _ = await _seed(backend)
    paying = await _open(backend, session_id, "c-1")
    await backend.add_pending_payment(paying.id)

    assert await backend.orders.expire_due(now=DUE, limit=10) == ExpiredReservations()
    assert await _stock(backend, b07) == StockLevel(10, reserved=1)


async def test_a_reservation_ending_exactly_now_is_not_due_yet(
    backend: ReservationBackend,
) -> None:
    session_id, _, _ = await _seed(backend)
    await _open(backend, session_id, "c-1")

    assert await backend.orders.expire_due(now=NOW + TTL, limit=10) == ExpiredReservations()


async def test_the_sweep_takes_the_lowest_ids_first_within_its_limit(
    backend: ReservationBackend,
) -> None:
    session_id, b07, _ = await _seed(backend)
    ids = [(await _open(backend, session_id, f"c-{n}")).id for n in range(3)]

    first = await backend.orders.expire_due(now=DUE, limit=2)
    rest = await backend.orders.expire_due(now=DUE, limit=2)

    assert [order.id for order in first.orders] == ids[:2]
    assert first.stock == {b07: StockLevel(10, reserved=1)}
    assert [order.id for order in rest.orders] == ids[2:]
    assert rest.stock == {b07: StockLevel(10)}


async def test_two_sweeps_at_once_expire_each_order_exactly_once(
    backend: ReservationBackend,
) -> None:
    session_id, b07, a12 = await _seed(backend)
    for n in range(10):
        await _open(backend, session_id, f"b-{n}")
        await _open(backend, session_id, f"a-{n}", code="A12", variant="M")

    async def sweep() -> list[UUID]:
        swept: list[UUID] = []
        while batch := (await backend.orders.expire_due(now=DUE, limit=3)).orders:
            swept.extend(order.id for order in batch)
        return swept

    first, second = await asyncio.gather(sweep(), sweep())

    assert not set(first) & set(second)
    assert len(first) + len(second) == 20
    assert await _stock(backend, b07) == StockLevel(10)
    assert await _stock(backend, a12) == StockLevel(10)


@pytest.mark.parametrize("limit", [0, -1])
async def test_a_sweep_takes_at_least_one_order(backend: ReservationBackend, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        await backend.orders.expire_due(now=DUE, limit=limit)
