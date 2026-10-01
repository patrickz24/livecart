"""``PaymentRepository`` and the buyer's address on every backend: the fake, then PostgreSQL.

StartCheckout's first step: an attempt opens for the order's total, one
pending at a time, at most ``max_attempts``, and only while the order waits
for its payment and its reservation runs; opening it extends the
reservation to at least ``now + grace``. The sweep and the opening of an
attempt never both win an order. An attempt is found by its provider's
session, and settles once, by compare-and-set.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    AttemptOpening,
    AttemptOpeningKind,
    IntentSource,
    OrderRecord,
    PaymentProviderName,
    ReservationRequest,
    ShippingAddress,
    StockLevel,
)
from livecart_core.application.ports import OrderClosed, RecordNotFound
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus, PaymentStatus, TransitionOutcome
from livecart_core.testing.backends import ReservationBackend
from livecart_core.testing.fakes import FAKE_CLOCK_START

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
GRACE = timedelta(seconds=600)
UNKNOWN = UUID(int=404)
RETURN_URL = "livecart://payment-return"
ADDRESS = ShippingAddress(
    full_name="Camille Martin",
    line1="12 rue des Lilas",
    postal_code="69003",
    city="Lyon",
    country="FR",
)


async def _session(backend: ReservationBackend, *, stock_total: int = 10) -> tuple[UUID, UUID]:
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


async def _open(
    backend: ReservationBackend,
    order_id: UUID,
    *,
    at: timedelta = timedelta(0),
    max_attempts: int = 3,
) -> AttemptOpening:
    return await backend.payments.open_attempt(
        order_id,
        provider=PaymentProviderName.MOCK,
        return_url=RETURN_URL,
        now=NOW + at,
        grace=GRACE,
        max_attempts=max_attempts,
    )


async def _reserved_until(backend: ReservationBackend, order_id: UUID) -> object:
    order = await backend.orders.get(order_id)
    assert order is not None
    return order.reserved_until


# --- Opening an attempt ----------------------------------------------------------------


async def test_a_first_attempt_opens_for_the_order_total(backend: ReservationBackend) -> None:
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")

    opening = await _open(backend, order.id)

    payment = opening.payment
    assert opening.kind is AttemptOpeningKind.OPENED
    assert payment is not None
    assert (payment.order_id, payment.attempt_no) == (order.id, 1)
    assert payment.status is PaymentStatus.PENDING
    assert payment.amount == Money(2_500, "EUR")
    assert (payment.provider, payment.return_url) == (PaymentProviderName.MOCK, RETURN_URL)
    assert payment.created_at == NOW
    assert (payment.provider_ref, payment.redirect_url) == (None, None)
    assert await backend.payments.get_pending(order.id) == payment
    assert await backend.payments.latest(order.id) == payment


async def test_opening_extends_the_reservation_to_the_grace_period_at_least(
    backend: ReservationBackend,
) -> None:
    session_id, _ = await _session(backend)
    early = await _order(backend, session_id, "c-1")
    late = await _order(backend, session_id, "c-2")

    await _open(backend, early.id)
    await _open(backend, late.id, at=TTL - timedelta(seconds=60))

    # Early: NOW + GRACE is before NOW + TTL, which stays; late: extended.
    assert await _reserved_until(backend, early.id) == NOW + TTL
    assert await _reserved_until(backend, late.id) == NOW + TTL - timedelta(seconds=60) + GRACE


async def test_a_second_click_gets_the_pending_attempt_back_without_extending(
    backend: ReservationBackend,
) -> None:
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")
    first = await _open(backend, order.id, at=TTL - timedelta(seconds=60))
    assert first.payment is not None
    recorded = await backend.payments.record_checkout(
        first.payment.id, provider_ref="cs_1", redirect_url="https://payments.invalid/1"
    )
    extended = await _reserved_until(backend, order.id)

    again = await _open(backend, order.id, at=TTL + timedelta(seconds=120))

    assert again == AttemptOpening(AttemptOpeningKind.PENDING, recorded)
    assert await _reserved_until(backend, order.id) == extended


async def test_a_new_attempt_follows_a_settled_one_up_to_the_limit(
    backend: ReservationBackend,
) -> None:
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")
    opened = []
    for attempt_no in (1, 2):
        opening = await _open(backend, order.id, max_attempts=2)
        assert opening.payment is not None
        assert opening.payment.attempt_no == attempt_no
        opened.append(opening.payment)
        await backend.settle_payment(opening.payment.id, PaymentStatus.EXPIRED)

    exhausted = await _open(backend, order.id, max_attempts=2)

    assert exhausted == AttemptOpening(AttemptOpeningKind.EXHAUSTED)
    assert await backend.payments.get_pending(order.id) is None
    latest = await backend.payments.latest(order.id)
    assert latest is not None
    assert (latest.id, latest.status) == (opened[1].id, PaymentStatus.EXPIRED)


async def test_an_order_past_its_reservation_expired_or_paid_is_not_payable(
    backend: ReservationBackend,
) -> None:
    session_id, _ = await _session(backend)
    due = await _order(backend, session_id, "c-1")
    expired = await _order(backend, session_id, "c-2")
    await backend.expire_order(expired.id)

    # A reservation ending exactly now has ended.
    assert (await _open(backend, due.id, at=TTL)).kind is AttemptOpeningKind.NOT_PAYABLE
    assert (await _open(backend, expired.id)).kind is AttemptOpeningKind.NOT_PAYABLE
    assert await backend.payments.latest(due.id) is None
    assert await backend.payments.latest(expired.id) is None
    assert await _reserved_until(backend, due.id) == NOW + TTL


async def test_an_order_that_used_its_attempts_is_exhausted_before_it_is_payable_again(
    backend: ReservationBackend,
) -> None:
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")
    opening = await _open(backend, order.id, max_attempts=1)
    assert opening.payment is not None
    await backend.settle_payment(opening.payment.id, PaymentStatus.FAILED)
    reserved_until = await _reserved_until(backend, order.id)

    assert (await _open(backend, order.id, max_attempts=1)).kind is AttemptOpeningKind.EXHAUSTED
    # Refused: the reservation is not extended again.
    assert await _reserved_until(backend, order.id) == reserved_until


async def test_opening_needs_a_known_order_and_sensible_bounds(
    backend: ReservationBackend,
) -> None:
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")

    with pytest.raises(RecordNotFound):
        await _open(backend, UNKNOWN)
    with pytest.raises(ValueError, match="max_attempts"):
        await _open(backend, order.id, max_attempts=0)
    with pytest.raises(ValueError, match="grace"):
        await backend.payments.open_attempt(
            order.id,
            provider=PaymentProviderName.MOCK,
            return_url=RETURN_URL,
            now=NOW,
            grace=timedelta(0),
            max_attempts=3,
        )
    assert await backend.payments.latest(order.id) is None


async def test_concurrent_clicks_open_one_attempt(backend: ReservationBackend) -> None:
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")

    openings = await asyncio.gather(*(_open(backend, order.id) for _ in range(10)))

    kinds = sorted(opening.kind for opening in openings)
    assert kinds == [AttemptOpeningKind.OPENED] + [AttemptOpeningKind.PENDING] * 9
    assert len({opening.payment.id for opening in openings if opening.payment}) == 1


async def test_the_sweep_and_an_opening_never_both_win_an_order(
    backend: ReservationBackend,
) -> None:
    session_id, product_id = await _session(backend, stock_total=40)
    orders = [await _order(backend, session_id, f"c-{n}") for n in range(20)]
    # The opening's reservation still runs; the sweep's clock is past it.
    opening_at, sweep_at = TTL - timedelta(seconds=1), NOW + TTL + timedelta(seconds=1)

    async def sweep() -> set[UUID]:
        expired: set[UUID] = set()
        while batch := (await backend.orders.expire_due(now=sweep_at, limit=3)).orders:
            expired |= {order.id for order in batch}
        return expired

    results = await asyncio.gather(
        sweep(), *(_open(backend, order.id, at=opening_at) for order in orders)
    )

    expired, openings = results[0], results[1:]
    assert isinstance(expired, set)
    for order, opening in zip(orders, openings, strict=True):
        assert isinstance(opening, AttemptOpening)
        stored = await backend.orders.get(order.id)
        assert stored is not None
        if order.id in expired:
            assert opening.kind is AttemptOpeningKind.NOT_PAYABLE
            assert stored.status is OrderStatus.EXPIRED
        else:
            assert opening.kind is AttemptOpeningKind.OPENED
            assert stored.status is OrderStatus.PENDING_PAYMENT
            assert stored.reserved_until == NOW + opening_at + GRACE
    product = await backend.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(40, reserved=2 * (len(orders) - len(expired)))


# --- The provider's session ------------------------------------------------------------


async def test_the_provider_session_is_recorded_once(backend: ReservationBackend) -> None:
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")
    opening = await _open(backend, order.id)
    assert opening.payment is not None
    payment_id = opening.payment.id

    first = await backend.payments.record_checkout(
        payment_id, provider_ref="cs_1", redirect_url="https://payments.invalid/1"
    )
    again = await backend.payments.record_checkout(
        payment_id, provider_ref="cs_2", redirect_url="https://payments.invalid/2"
    )

    assert (first.provider_ref, first.redirect_url) == ("cs_1", "https://payments.invalid/1")
    assert again == first
    assert await backend.payments.get_pending(order.id) == first
    with pytest.raises(RecordNotFound):
        await backend.payments.record_checkout(
            UNKNOWN, provider_ref="cs_3", redirect_url="https://payments.invalid/3"
        )


async def test_an_order_without_attempts_has_no_pending_nor_latest_one(
    backend: ReservationBackend,
) -> None:
    assert await backend.payments.get_pending(UNKNOWN) is None
    assert await backend.payments.latest(UNKNOWN) is None


async def test_an_attempt_is_found_by_its_provider_session(backend: ReservationBackend) -> None:
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")
    opening = await _open(backend, order.id)
    assert opening.payment is not None
    assert await backend.payments.find_by_provider_ref("cs_1") is None

    recorded = await backend.payments.record_checkout(
        opening.payment.id, provider_ref="cs_1", redirect_url="https://payments.invalid/1"
    )

    assert await backend.payments.find_by_provider_ref("cs_1") == recorded
    assert await backend.payments.find_by_provider_ref("cs_404") is None


# --- Settling an attempt ---------------------------------------------------------------


async def _pending(backend: ReservationBackend) -> UUID:
    """A pending attempt of a new order, on the provider's session cs_1."""
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")
    opening = await _open(backend, order.id)
    assert opening.payment is not None
    await backend.payments.record_checkout(
        opening.payment.id, provider_ref="cs_1", redirect_url="https://payments.invalid/1"
    )
    return opening.payment.id


async def _status(backend: ReservationBackend, payment_id: UUID) -> tuple[object, object]:
    """The attempt's status and payment ref, read back by its session."""
    payment = await backend.payments.find_by_provider_ref("cs_1")
    assert payment is not None
    assert payment.id == payment_id
    return payment.status, payment.provider_payment_ref


async def test_a_pending_attempt_settles_once(backend: ReservationBackend) -> None:
    payment_id = await _pending(backend)

    first = await backend.payments.set_status(
        payment_id, PaymentStatus.SUCCEEDED, provider_payment_ref="pi_1"
    )
    again = await backend.payments.set_status(
        payment_id, PaymentStatus.SUCCEEDED, provider_payment_ref="pi_2"
    )

    assert (first, again) == (TransitionOutcome.APPLIED, TransitionOutcome.NOOP)
    # The repeat changes nothing, the payment's ref included.
    assert await _status(backend, payment_id) == (PaymentStatus.SUCCEEDED, "pi_1")
    with pytest.raises(InvalidTransition):
        await backend.payments.set_status(payment_id, PaymentStatus.EXPIRED)
    with pytest.raises(InvalidTransition):
        await backend.payments.set_status(payment_id, PaymentStatus.PENDING)
    with pytest.raises(RecordNotFound):
        await backend.payments.set_status(UNKNOWN, PaymentStatus.EXPIRED)


async def test_a_settlement_without_a_payment_ref_keeps_the_attempt_without_one(
    backend: ReservationBackend,
) -> None:
    payment_id = await _pending(backend)

    outcome = await backend.payments.set_status(payment_id, PaymentStatus.EXPIRED)

    assert outcome is TransitionOutcome.APPLIED
    assert await _status(backend, payment_id) == (PaymentStatus.EXPIRED, None)


async def test_of_two_settlements_at_once_one_applies(backend: ReservationBackend) -> None:
    payment_id = await _pending(backend)

    results = await asyncio.gather(
        backend.payments.set_status(payment_id, PaymentStatus.SUCCEEDED),
        backend.payments.set_status(payment_id, PaymentStatus.EXPIRED),
        return_exceptions=True,
    )

    applied = [result for result in results if result is TransitionOutcome.APPLIED]
    refused = [result for result in results if isinstance(result, InvalidTransition)]
    assert (len(applied), len(refused)) == (1, 1)
    status, _ = await _status(backend, payment_id)
    assert status in (PaymentStatus.SUCCEEDED, PaymentStatus.EXPIRED)


# --- The buyer's address ---------------------------------------------------------------


async def test_a_pending_order_records_its_shipping_address(backend: ReservationBackend) -> None:
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")
    moved = ADDRESS.model_copy(update={"line1": "3 place Bellecour", "line2": "Bât. B"})

    first = await backend.orders.set_shipping_address(order.id, ADDRESS)
    second = await backend.orders.set_shipping_address(order.id, moved)

    assert first.shipping_address == ADDRESS
    assert second.shipping_address == moved
    assert await backend.orders.get(order.id) == second
    found = await backend.orders.find_by_reference(order.reference)
    assert found is not None
    assert found.order.shipping_address == moved


async def test_a_closed_order_keeps_its_address(backend: ReservationBackend) -> None:
    session_id, _ = await _session(backend)
    order = await _order(backend, session_id, "c-1")
    await backend.orders.set_shipping_address(order.id, ADDRESS)
    await backend.expire_order(order.id)

    with pytest.raises(OrderClosed):
        await backend.orders.set_shipping_address(
            order.id, ADDRESS.model_copy(update={"city": "X"})
        )
    with pytest.raises(RecordNotFound):
        await backend.orders.set_shipping_address(UNKNOWN, ADDRESS)
    stored = await backend.orders.get(order.id)
    assert stored is not None
    assert stored.shipping_address == ADDRESS
