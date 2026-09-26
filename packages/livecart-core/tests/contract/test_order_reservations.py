"""Unit 3, ``OrderReservations.reserve``, on every backend: the in-memory fake, then PostgreSQL.

Each scenario runs twice through the ``backend`` fixture; the PostgreSQL run
is marked ``integration``. The fake and the SQL adapter keep the same promises
(ADR 0002), and these scenarios are those promises.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    IntentSource,
    ParseKind,
    ParseSummary,
    ReservationRequest,
    ResolvedIntent,
    StockLevel,
)
from livecart_core.application.ports import RecordNotFound, ServiceUnavailable
from livecart_core.application.reserve_stock import ReserveStock
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.outcomes import AmbiguityReason, IntentOutcome
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.reference import REFERENCE_SPACE, OrderReference, encode_reference
from livecart_core.domain.state_machines import CommentStatus, LiveSessionStatus, OrderStatus
from livecart_core.testing.backends import (
    ORDER_TOKENS,
    BackendFactory,
    ReservationBackend,
)
from livecart_core.testing.fakes import FAKE_CLOCK_START, FakeClock
from livecart_core.testing.randomness import SequenceRandomSource

NOW = FAKE_CLOCK_START
TTL = timedelta(seconds=900)
UNKNOWN = UUID(int=404)


def _request(
    comment_id: UUID,
    session_id: UUID,
    code: str = "B07",
    *,
    variant: str = "",
    quantity: int = 1,
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
        reserved_until=NOW + TTL,
    )


async def _seed(backend: ReservationBackend, *, stock_total: int = 10) -> tuple[UUID, UUID, UUID]:
    """A live session selling B07 (no variant) and A12 M: the session, its seller, B07."""
    session = await backend.add_live_session()
    b07 = await backend.add_product(
        session.id, "B07", price=Money(1_250, "EUR"), stock_total=stock_total
    )
    await backend.add_product(session.id, "A12", variant="M", stock_total=stock_total)
    return session.id, session.seller_id, b07.id


async def test_a_reservation_opens_a_pending_order_for_the_author(
    backend: ReservationBackend,
) -> None:
    session_id, seller_id, product_id = await _seed(backend)
    comment_id = await backend.add_comment("comment-1", author="buyer-7")

    result = await backend.orders.reserve(_request(comment_id, session_id, quantity=2))

    assert result is not None
    order = result.order
    assert result.intent.outcome is IntentOutcome.RESERVED
    assert result.intent.product_id == product_id
    assert result.intent.evidence == ("je prends", "B07")
    assert order is not None
    assert order.status is OrderStatus.PENDING_PAYMENT
    assert order.intent_id == result.intent.id
    assert order.buyer_platform_user_id == "buyer-7"
    assert order.seller_id == seller_id
    assert (order.unit_price, order.total) == (Money(1_250, "EUR"), Money(2_500, "EUR"))
    assert order.reserved_until == NOW + TTL
    assert order.created_at == NOW
    assert result.stock == StockLevel(10, reserved=2)
    product = await backend.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10, reserved=2)
    assert await backend.comment_state(comment_id) == (CommentStatus.PROCESSED, session_id)
    assert await backend.orders.find_reservation(comment_id) == result
    assert await backend.orders.get(order.id) == order
    assert await backend.orders.get_by_reference(order.reference) == order


@pytest.mark.parametrize(
    "parse",
    [
        ParseSummary(ParseKind.MATCH),
        ParseSummary(ParseKind.AMBIGUOUS, AmbiguityReason.PRODUCT_BY_NAME),
        None,
    ],
)
async def test_the_settlement_records_what_the_parser_said(
    backend: ReservationBackend, parse: ParseSummary | None
) -> None:
    session_id, _, _ = await _seed(backend)
    comment_id = await backend.add_comment("comment-1")

    await backend.orders.reserve(replace(_request(comment_id, session_id), parse=parse))

    comment = await backend.comments.get(comment_id)
    assert comment is not None
    assert comment.status is CommentStatus.PROCESSED
    assert comment.parse == parse


async def test_the_buyer_is_marked_notified_once(backend: ReservationBackend) -> None:
    session_id, _, _ = await _seed(backend)
    result = await backend.orders.reserve(
        _request(await backend.add_comment("comment-1"), session_id)
    )
    assert result is not None
    assert result.order is not None
    assert result.order.buyer_notified_at is None

    first = await backend.orders.mark_buyer_notified(result.order.id, now=NOW)
    again = await backend.orders.mark_buyer_notified(
        result.order.id, now=NOW + timedelta(seconds=5)
    )

    assert first.buyer_notified_at == again.buyer_notified_at == NOW
    assert await backend.orders.get(result.order.id) == again
    with pytest.raises(RecordNotFound):
        await backend.orders.mark_buyer_notified(UNKNOWN, now=NOW)


async def test_short_stock_records_an_out_of_stock_intent(backend: ReservationBackend) -> None:
    session_id, _, _ = await _seed(backend, stock_total=1)
    comment_id = await backend.add_comment("comment-1")

    result = await backend.orders.reserve(_request(comment_id, session_id, quantity=2))

    assert result is not None
    assert result.intent.outcome is IntentOutcome.OUT_OF_STOCK
    assert result.order is None
    assert result.stock == StockLevel(1)
    assert await backend.count("orders") == 0
    assert (await backend.comment_state(comment_id))[0] is CommentStatus.PROCESSED


async def test_a_session_no_longer_live_reserves_nothing(backend: ReservationBackend) -> None:
    session_id, _, _ = await _seed(backend)
    comment_id = await backend.add_comment("comment-1")
    await backend.set_session_status(session_id, LiveSessionStatus.ENDED)

    result = await backend.orders.reserve(_request(comment_id, session_id))

    assert result is not None
    assert result.intent.outcome is IntentOutcome.OUT_OF_STOCK
    assert result.stock == StockLevel(10)


async def test_one_pending_order_per_buyer_and_product(backend: ReservationBackend) -> None:
    session_id, _, _ = await _seed(backend)
    first = await backend.orders.reserve(
        _request(await backend.add_comment("comment-1"), session_id)
    )
    same = await backend.orders.reserve(
        _request(await backend.add_comment("comment-2"), session_id)
    )
    other_product = await backend.orders.reserve(
        _request(await backend.add_comment("comment-3"), session_id, "A12", variant="M")
    )
    other_buyer = await backend.orders.reserve(
        _request(await backend.add_comment("comment-4", author="buyer-2"), session_id)
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
    assert await backend.count("orders") == 3
    assert await backend.count("purchase_intents") == 4


async def test_short_stock_is_checked_before_the_pending_order(
    backend: ReservationBackend,
) -> None:
    session_id, _, _ = await _seed(backend, stock_total=1)
    await backend.orders.reserve(_request(await backend.add_comment("comment-1"), session_id))

    again = await backend.orders.reserve(
        _request(await backend.add_comment("comment-2"), session_id)
    )

    assert again is not None
    assert again.intent.outcome is IntentOutcome.OUT_OF_STOCK


async def test_an_expired_order_frees_the_buyer_for_a_new_one(backend: ReservationBackend) -> None:
    session_id, _, product_id = await _seed(backend)
    first = await backend.orders.reserve(
        _request(await backend.add_comment("comment-1"), session_id)
    )
    assert first is not None
    assert first.order is not None
    await backend.expire_order(first.order.id)

    again = await backend.orders.reserve(
        _request(await backend.add_comment("comment-2"), session_id)
    )

    assert again is not None
    assert again.intent.outcome is IntentOutcome.RESERVED
    product = await backend.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10, reserved=1)


async def test_a_settled_comment_reserves_nothing_and_writes_nothing(
    backend: ReservationBackend,
) -> None:
    session_id, _, product_id = await _seed(backend)
    processed = await backend.add_comment("comment-1")
    rejected = await backend.add_comment("comment-2")
    await backend.orders.reserve(_request(processed, session_id))
    await backend.reject_comment(rejected)

    assert await backend.orders.reserve(_request(processed, session_id)) is None
    assert await backend.orders.reserve(_request(rejected, session_id)) is None
    assert (await backend.count("purchase_intents"), await backend.count("orders")) == (1, 1)
    product = await backend.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10, reserved=1)


async def test_twenty_parallel_buyers_never_oversell(backend: ReservationBackend) -> None:
    session_id, _, product_id = await _seed(backend, stock_total=5)
    comments = [await backend.add_comment(f"comment-{n}", author=f"buyer-{n}") for n in range(20)]

    results = await asyncio.gather(
        *(backend.orders.reserve(_request(comment_id, session_id)) for comment_id in comments)
    )

    outcomes = [result.intent.outcome for result in results if result is not None]
    assert outcomes.count(IntentOutcome.RESERVED) == 5
    assert outcomes.count(IntentOutcome.OUT_OF_STOCK) == 15
    product = await backend.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(5, reserved=5)


async def test_twenty_parallel_comments_of_one_buyer_give_one_order(
    backend: ReservationBackend,
) -> None:
    session_id, _, _ = await _seed(backend)
    comments = [await backend.add_comment(f"comment-{n}") for n in range(20)]

    results = await asyncio.gather(
        *(backend.orders.reserve(_request(comment_id, session_id)) for comment_id in comments)
    )

    outcomes = [result.intent.outcome for result in results if result is not None]
    assert outcomes.count(IntentOutcome.RESERVED) == 1
    assert outcomes.count(IntentOutcome.ALREADY_RESERVED) == 19
    assert await backend.count("orders") == 1


async def test_unit_3_needs_a_known_comment_session_and_product(
    backend: ReservationBackend,
) -> None:
    session_id, _, _ = await _seed(backend)
    comment_id = await backend.add_comment("comment-1")

    with pytest.raises(RecordNotFound, match="comment"):
        await backend.orders.reserve(_request(UNKNOWN, session_id))
    with pytest.raises(RecordNotFound, match="live session"):
        await backend.orders.reserve(_request(comment_id, UNKNOWN))
    with pytest.raises(RecordNotFound, match="product"):
        await backend.orders.reserve(_request(comment_id, session_id, "Z99"))
    assert (await backend.comment_state(comment_id))[0] is CommentStatus.RECEIVED
    assert await backend.count("purchase_intents") == 0


async def test_lookups_of_what_does_not_exist(backend: ReservationBackend) -> None:
    await _seed(backend)

    assert await backend.orders.find_reservation(UNKNOWN) is None
    assert await backend.orders.get(UNKNOWN) is None
    assert await backend.orders.get_by_reference(OrderReference("LC-000000")) is None


async def test_an_out_of_stock_reservation_reads_back_without_order(
    backend: ReservationBackend,
) -> None:
    session_id, _, _ = await _seed(backend, stock_total=0)
    comment_id = await backend.add_comment("comment-1")
    await backend.orders.reserve(_request(comment_id, session_id))

    stored = await backend.orders.find_reservation(comment_id)

    assert stored is not None
    assert stored.intent.outcome is IntentOutcome.OUT_OF_STOCK
    assert stored.order is None
    assert stored.stock == StockLevel(0)


# --- Reference draws, through the use case -----------------------------------------------

B07 = ResolvedIntent(
    product_code=ProductCode("B07"),
    variant=Variant(),
    quantity=Quantity(1),
    source=IntentSource.PARSER,
    confidence=Decimal("1.00"),
    evidence=("je prends", "B07"),
)


async def test_a_colliding_reference_is_drawn_again(make_backend: BackendFactory) -> None:
    # The second order draws the first one's reference, then a free one.
    source = SequenceRandomSource([5, 5, 6])
    backend = make_backend(random_source=source)
    session_id, _, product_id = await _seed(backend)
    reserve = ReserveStock(backend.orders, FakeClock(), reservation_ttl=TTL)

    first = await reserve(await backend.add_comment("comment-1"), session_id, B07)
    # No IntegrityError reaches the use case: the collision is retried inside the unit.
    second = await reserve(
        await backend.add_comment("comment-2", author="buyer-2"), session_id, B07
    )

    assert first is not None
    assert second is not None
    assert first.order is not None
    assert second.order is not None
    assert first.order.reference == encode_reference(5)
    assert second.order.reference == encode_reference(6)
    assert source.requested_bounds == [REFERENCE_SPACE] * 3
    # The retry kept the reservation made before it.
    assert second.intent.outcome is IntentOutcome.RESERVED
    assert await backend.orders.get_by_reference(encode_reference(6)) == second.order
    product = await backend.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10, reserved=2)
    assert (await backend.count("orders"), await backend.count("purchase_intents")) == (2, 2)


async def test_three_collisions_leave_everything_as_it_was(make_backend: BackendFactory) -> None:
    backend = make_backend(random_source=SequenceRandomSource([5, 5, 5, 5]))
    session_id, _, product_id = await _seed(backend)
    reserve = ReserveStock(backend.orders, FakeClock(), reservation_ttl=TTL)
    await reserve(await backend.add_comment("comment-1"), session_id, B07)
    comment_id = await backend.add_comment("comment-2", author="buyer-2")

    with pytest.raises(ServiceUnavailable, match="3 order reference draws collided"):
        await reserve(comment_id, session_id, B07)

    # A transient failure: the comment stays received, for its redelivery to try again.
    assert await backend.comment_state(comment_id) == (CommentStatus.RECEIVED, None)
    assert await backend.orders.find_reservation(comment_id) is None
    assert (await backend.count("orders"), await backend.count("purchase_intents")) == (1, 1)
    product = await backend.products.get(product_id)
    assert product is not None
    assert product.stock == StockLevel(10, reserved=1)


async def test_an_already_reserved_intent_reads_back_without_order(
    backend: ReservationBackend,
) -> None:
    session_id, _, _ = await _seed(backend)
    await backend.orders.reserve(_request(await backend.add_comment("comment-1"), session_id))
    comment_id = await backend.add_comment("comment-2")
    result = await backend.orders.reserve(_request(comment_id, session_id))

    stored = await backend.orders.find_reservation(comment_id)

    assert stored == result
    assert stored is not None
    assert stored.intent.outcome is IntentOutcome.ALREADY_RESERVED
    assert stored.order is None


# --- What the buyer reads --------------------------------------------------------------


async def test_a_buyer_finds_an_order_by_its_reference_with_its_product(
    backend: ReservationBackend,
) -> None:
    session_id, _, _ = await _seed(backend)
    comment_id = await backend.add_comment("comment-1")
    result = await backend.orders.reserve(
        _request(comment_id, session_id, "A12", variant="M", quantity=2)
    )
    assert result is not None
    assert result.order is not None
    order = result.order

    found = await backend.orders.find_by_reference(order.reference)

    assert found is not None
    assert found.order == order
    assert (found.product_code, found.product_variant, found.product_name) == (
        ProductCode("A12"),
        Variant("M"),
        "Produit",
    )
    # The stored hash proves the order's own token, and no other.
    assert ORDER_TOKENS.verify(ORDER_TOKENS.token_for(order.id), found.access_token_hash)
    assert not ORDER_TOKENS.verify(ORDER_TOKENS.token_for(UNKNOWN), found.access_token_hash)
    assert "access_token_hash" not in repr(found)
    assert await backend.orders.find_by_reference(OrderReference("LC-000000")) is None
