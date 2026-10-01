from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    AttemptOpening,
    AttemptOpeningKind,
    IntentSource,
    PaymentProviderName,
    ReservationRequest,
)
from livecart_core.application.ports import RecordNotFound
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.money import Money
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import (
    OrderStatus,
    PaymentStatus,
    TransitionOutcome,
)
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import FAKE_CLOCK_START, InMemoryDatabase

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
GRACE = timedelta(seconds=600)
UNKNOWN = UUID(int=404)
RETURN_URL = "livecart://payment-return"


async def _order(database: InMemoryDatabase, comment: str = "comment-1") -> UUID:
    """A pending order of 2 x B07 at 12.50 EUR, reserved until NOW + TTL."""
    session = next(iter(database.tables.sessions.values()), None) or database.add_live_session()
    if database.find_product(session.id, ProductCode("B07"), Variant()) is None:
        database.add_product(session.id, "B07", price=Money(1_250, "EUR"))
    envelope = queued_comment(external_comment_id=comment, author=f"buyer-of-{comment}")
    claim = await database.comments.claim(
        envelope, holder="worker-1", now=NOW, lease=timedelta(seconds=30)
    )
    result = await database.orders.reserve(
        ReservationRequest(
            comment_id=claim.comment.id,
            session_id=session.id,
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
    return result.order.id


async def _open(
    database: InMemoryDatabase,
    order_id: UUID,
    *,
    now: timedelta = timedelta(0),
    max_attempts: int = 3,
) -> AttemptOpening:
    return await database.payments.open_attempt(
        order_id,
        provider=PaymentProviderName.MOCK,
        return_url=RETURN_URL,
        now=NOW + now,
        grace=GRACE,
        max_attempts=max_attempts,
    )


# --- Payments --------------------------------------------------------------------------
# What every backend does is in tests/contract/test_payment_attempts.py, and
# the settlement of an attempt by a payment event in test_webhook_event_registry.py.


async def test_an_order_past_its_reservation_or_paid_is_not_payable() -> None:
    database = InMemoryDatabase()
    due, paid = await _order(database, "comment-1"), await _order(database, "comment-2")
    order = database.tables.orders[paid]
    database.tables.orders[paid] = replace(order, status=OrderStatus.PAID, paid_at=NOW)

    assert (await _open(database, due, now=TTL)).kind is AttemptOpeningKind.NOT_PAYABLE
    assert (await _open(database, paid)).kind is AttemptOpeningKind.NOT_PAYABLE
    assert database.tables.payments == {}


async def test_a_pending_attempt_settles_once() -> None:
    database = InMemoryDatabase()
    opening = await _open(database, await _order(database))
    assert opening.payment is not None
    payment_id = opening.payment.id

    succeeded = await database.payments.set_status(
        payment_id, PaymentStatus.SUCCEEDED, provider_payment_ref="fake_pi_1"
    )
    replayed = await database.payments.set_status(payment_id, PaymentStatus.SUCCEEDED)

    stored = database.tables.payments[payment_id]
    assert (succeeded, replayed) == (TransitionOutcome.APPLIED, TransitionOutcome.NOOP)
    assert (stored.status, stored.provider_payment_ref) == (PaymentStatus.SUCCEEDED, "fake_pi_1")
    with pytest.raises(InvalidTransition):
        await database.payments.set_status(payment_id, PaymentStatus.EXPIRED)
    with pytest.raises(RecordNotFound):
        await database.payments.set_status(UNKNOWN, PaymentStatus.FAILED)


async def test_a_settled_attempt_without_reference_keeps_the_one_it_had() -> None:
    database = InMemoryDatabase()
    opening = await _open(database, await _order(database))
    assert opening.payment is not None

    await database.payments.set_status(opening.payment.id, PaymentStatus.FAILED)

    assert database.tables.payments[opening.payment.id].provider_payment_ref is None


# --- Shipments -------------------------------------------------------------------------
# What every backend does is in tests/contract/test_shipment_repository.py.


async def test_the_shipment_claims_refuse_an_empty_batch_a_naive_time_or_no_lease() -> None:
    database = InMemoryDatabase()

    with pytest.raises(ValueError, match="limit"):
        await database.shipments.unshipped_orders(paid_before=NOW, limit=0)
    with pytest.raises(ValueError, match="naive"):
        await database.shipments.unshipped_orders(paid_before=NOW.replace(tzinfo=None), limit=1)
    with pytest.raises(ValueError, match="limit"):
        await database.shipments.lease_for_tracking(now=NOW, limit=0, lease=timedelta(seconds=1))
    with pytest.raises(ValueError, match="lease"):
        await database.shipments.lease_for_tracking(now=NOW, limit=1, lease=timedelta(0))
