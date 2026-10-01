"""``CheckoutExpiry`` on every backend: the in-memory fake, then PostgreSQL.

The later steps of the expiry sweep: the due orders whose attempt is still
pending are leased to one sweep at a time, which asks the provider to expire
the attempt's session; once it has, releasing the order expires the attempt
and the order and returns the stock, exactly once, whatever the provider's
own events did meanwhile.
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    IntentSource,
    OrderRecord,
    PaymentEvent,
    PaymentProviderName,
    PaymentRecord,
    ReleasedCheckout,
    ReservationRequest,
    StockLevel,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.payments import PaymentEffect
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus
from livecart_core.testing.backends import ReservationBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
GRACE = timedelta(seconds=600)
LEASE = timedelta(seconds=30)
DUE = NOW + TTL + timedelta(seconds=1)
UNKNOWN = UUID(int=404)


async def _product(backend: ReservationBackend, *, stock_total: int = 10) -> tuple[UUID, UUID]:
    """A live session selling B07 at 12.50 EUR: the session and the product."""
    session = await backend.add_live_session()
    product = await backend.add_product(
        session.id, "B07", price=Money(1_250, "EUR"), stock_total=stock_total
    )
    return session.id, product.id


async def _order(backend: ReservationBackend, session_id: UUID, comment: str) -> OrderRecord:
    """A pending order of 2 x B07 for a buyer of its own, reserved until NOW + TTL."""
    comment_id = await backend.add_comment(comment, author=f"buyer-of-{comment}")
    result = await backend.orders.reserve(
        ReservationRequest(
            comment_id=comment_id,
            session_id=session_id,
            product_code=ProductCode("B07"),
            variant=Variant(),
            quantity=Quantity(2),
            source=IntentSource.PARSER,
            confidence=Decimal("1.00"),
            evidence=("B07",),
            now=NOW,
            reserved_until=NOW + TTL,
        )
    )
    assert result is not None
    assert result.order is not None
    return result.order


async def _attempt(
    backend: ReservationBackend, order: OrderRecord, *, at: timedelta = timedelta(0)
) -> PaymentRecord:
    """The order's attempt, opened at NOW + ``at``, on the provider's session cs_<reference>."""
    opening = await backend.payments.open_attempt(
        order.id,
        provider=PaymentProviderName.MOCK,
        return_url="livecart://payment-return",
        now=NOW + at,
        grace=GRACE,
        max_attempts=3,
    )
    assert opening.payment is not None
    return await backend.payments.record_checkout(
        opening.payment.id,
        provider_ref=f"cs_{order.reference.value}",
        redirect_url=f"https://payments.invalid/{order.reference.value}",
    )


async def _paying(backend: ReservationBackend, session_id: UUID, comment: str) -> PaymentRecord:
    """A due order at DUE whose attempt, opened at NOW, is still pending."""
    return await _attempt(backend, await _order(backend, session_id, comment))


async def _stock(backend: ReservationBackend, product_id: UUID) -> StockLevel:
    product = await backend.products.get(product_id)
    assert product is not None
    return product.stock


async def _leased(
    backend: ReservationBackend, *, at: datetime = DUE, limit: int = 10
) -> list[UUID]:
    due = await backend.orders.lease_due_checkouts(now=at, limit=limit, lease=LEASE)
    return [checkout.order.id for checkout in due]


# --- Leasing ---------------------------------------------------------------------------


async def test_the_due_orders_with_a_pending_attempt_are_leased_with_it(
    backend: ReservationBackend,
) -> None:
    session_id, _ = await _product(backend)
    first = await _paying(backend, session_id, "c-1")
    second = await _paying(backend, session_id, "c-2")
    # Due, but without a payment in flight: the first step's.
    await _order(backend, session_id, "c-3")
    # Paying, but its reservation was extended past DUE when its attempt opened.
    await _attempt(
        backend, await _order(backend, session_id, "c-4"), at=TTL - timedelta(seconds=60)
    )
    # Due, its attempt failed: the first step's again.
    failed = await _paying(backend, session_id, "c-5")
    await backend.settle_payment(failed.id, PaymentStatus.FAILED)

    due = await backend.orders.lease_due_checkouts(now=DUE, limit=10, lease=LEASE)

    assert [(checkout.order.id, checkout.payment) for checkout in due] == [
        (first.order_id, first),
        (second.order_id, second),
    ]
    assert {checkout.lease_until for checkout in due} == {DUE + LEASE}
    assert {checkout.order.status for checkout in due} == {OrderStatus.PENDING_PAYMENT}


async def test_a_leased_order_is_left_to_its_sweep_until_the_lease_ends(
    backend: ReservationBackend,
) -> None:
    session_id, _ = await _product(backend)
    paying = await _paying(backend, session_id, "c-1")

    assert await _leased(backend) == [paying.order_id]
    assert await _leased(backend) == []
    assert await _leased(backend, at=DUE + LEASE) == []
    # Once the lease has ended, another sweep takes the order.
    assert await _leased(backend, at=DUE + LEASE + timedelta(seconds=1)) == [paying.order_id]


async def test_a_lease_takes_the_lowest_ids_first_within_its_limit(
    backend: ReservationBackend,
) -> None:
    session_id, _ = await _product(backend)
    ids = [(await _paying(backend, session_id, f"c-{n}")).order_id for n in range(3)]

    assert await _leased(backend, limit=2) == ids[:2]
    assert await _leased(backend, limit=2) == ids[2:]


async def test_a_reservation_ending_exactly_now_is_not_due_yet(
    backend: ReservationBackend,
) -> None:
    session_id, _ = await _product(backend)
    await _paying(backend, session_id, "c-1")

    assert await _leased(backend, at=NOW + TTL) == []


async def test_two_sweeps_at_once_lease_each_order_once(backend: ReservationBackend) -> None:
    session_id, _ = await _product(backend, stock_total=40)
    ids = {(await _paying(backend, session_id, f"c-{n}")).order_id for n in range(20)}

    async def sweep() -> list[UUID]:
        leased: list[UUID] = []
        while batch := await _leased(backend, limit=3):
            leased.extend(batch)
        return leased

    first, second = await asyncio.gather(sweep(), sweep())

    assert not set(first) & set(second)
    assert set(first) | set(second) == ids
    assert len(first) + len(second) == 20


@pytest.mark.parametrize(("limit", "lease"), [(0, LEASE), (1, timedelta(0))])
async def test_a_lease_takes_one_order_at_least_for_some_time(
    backend: ReservationBackend, limit: int, lease: timedelta
) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        await backend.orders.lease_due_checkouts(now=DUE, limit=limit, lease=lease)


# --- Releasing -------------------------------------------------------------------------


async def test_releasing_expires_the_attempt_and_the_order_and_returns_the_stock(
    backend: ReservationBackend,
) -> None:
    session_id, product_id = await _product(backend)
    paying = await _paying(backend, session_id, "c-1")
    await _leased(backend)

    released = await backend.orders.release_checkout(paying.order_id, paying.id, now=DUE)
    again = await backend.orders.release_checkout(paying.order_id, paying.id, now=DUE)

    order = released.order
    assert (order.id, order.status, order.expired_at) == (
        paying.order_id,
        OrderStatus.EXPIRED,
        DUE,
    )
    assert released == ReleasedCheckout(
        order, payment_changed=True, order_changed=True, stock=StockLevel(10)
    )
    assert await _stock(backend, product_id) == StockLevel(10)
    assert await backend.orders.get(order.id) == order
    latest = await backend.payments.latest(order.id)
    assert latest is not None
    assert latest.status is PaymentStatus.EXPIRED
    # A second release finds everything released: nothing moves twice.
    assert again == ReleasedCheckout(order)
    assert await _stock(backend, product_id) == StockLevel(10)
    assert await _leased(backend, at=DUE + 2 * LEASE) == []


async def test_an_attempt_the_provider_event_expired_first_is_released_once(
    backend: ReservationBackend,
) -> None:
    session_id, product_id = await _product(backend)
    paying = await _paying(backend, session_id, "c-1")
    # checkout.session.expired came first, through the registry.
    await backend.settle_payment(paying.id, PaymentStatus.EXPIRED)

    released = await backend.orders.release_checkout(paying.order_id, paying.id, now=DUE)

    assert (released.payment_changed, released.order_changed) == (False, True)
    assert released.order.status is OrderStatus.EXPIRED
    assert await _stock(backend, product_id) == StockLevel(10)


async def test_an_order_paid_meanwhile_is_never_released(backend: ReservationBackend) -> None:
    session_id, product_id = await _product(backend)
    paying = await _paying(backend, session_id, "c-1")
    assert paying.provider_ref is not None
    completed = PaymentEvent(
        provider=PaymentProviderName.MOCK,
        event_id="evt_1",
        event_type=CheckoutEventType.COMPLETED.value,
        checkout_ref=paying.provider_ref,
        payment_ref="pi_1",
        payment_status=CheckoutPaymentStatus.PAID,
        amount_minor=2_500,
        currency="EUR",
        occurred_at=NOW,
    )
    await backend.webhook_events.record(completed, now=NOW)
    await backend.webhook_events.settle(completed, PaymentEffect.PAID, now=NOW)

    released = await backend.orders.release_checkout(paying.order_id, paying.id, now=DUE)

    assert released == ReleasedCheckout(released.order)
    assert released.order.status is OrderStatus.PAID
    assert await _stock(backend, product_id) == StockLevel(10, reserved=0, sold=2)
    assert await _leased(backend) == []


async def test_an_order_whose_reservation_still_runs_keeps_it(backend: ReservationBackend) -> None:
    session_id, product_id = await _product(backend)
    paying = await _paying(backend, session_id, "c-1")

    released = await backend.orders.release_checkout(paying.order_id, paying.id, now=NOW + TTL)

    # The attempt expired; the order is the first step's once its reservation ends.
    assert (released.payment_changed, released.order_changed) == (True, False)
    assert released.order.status is OrderStatus.PENDING_PAYMENT
    assert await _stock(backend, product_id) == StockLevel(10, reserved=2)
    swept = await backend.orders.expire_due(now=DUE, limit=10)
    assert [order.id for order in swept.orders] == [paying.order_id]


async def test_releasing_needs_a_known_order(backend: ReservationBackend) -> None:
    with pytest.raises(RecordNotFound):
        await backend.orders.release_checkout(UNKNOWN, UNKNOWN, now=DUE)
