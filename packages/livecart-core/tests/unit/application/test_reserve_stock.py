from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import IntentSource, ResolvedIntent, StockLevel
from livecart_core.application.reserve_stock import ReserveStock
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.outcomes import IntentOutcome
from livecart_core.domain.parser import Confidence, Match, parse_comment
from livecart_core.domain.quantity import Quantity
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, demo_catalog, queued_comment
from livecart_core.testing.fakes import FakeClock, InMemoryDatabase

TTL = timedelta(seconds=900)
LEASE = timedelta(seconds=30)


def _intent(code: str = "A12", variant: str = "M", quantity: int = 1) -> ResolvedIntent:
    return ResolvedIntent(
        product_code=ProductCode(code),
        variant=Variant(variant),
        quantity=Quantity(quantity),
        source=IntentSource.LLM,
        confidence=Decimal("0.85"),
        evidence=("la robe", "M"),
    )


async def _setup(
    *, stock_total: int = 10
) -> tuple[InMemoryDatabase, FakeClock, ReserveStock, UUID, UUID]:
    """A live session selling the demo catalog, one claimed comment, and the use case."""
    database = InMemoryDatabase()
    clock = FakeClock()
    session = database.add_live_session()
    database.add_catalog(session.id, demo_catalog(), stock_total=stock_total)
    claim = await database.comments.claim(
        queued_comment(), consumer="worker-1", now=clock.now(), lease=LEASE
    )
    reserve = ReserveStock(database.orders, clock, reservation_ttl=TTL)
    return database, clock, reserve, session.id, claim.comment.id


async def test_the_reservation_holds_the_stock_for_the_ttl_from_the_clock() -> None:
    _, clock, reserve, session_id, comment_id = await _setup()
    clock.advance(42)

    result = await reserve(comment_id, session_id, _intent(quantity=2))

    assert result is not None
    assert result.order is not None
    assert result.order.created_at == clock.now()
    assert result.order.reserved_until == clock.now() + TTL
    assert result.stock == StockLevel(10, reserved=2)


async def test_the_intent_is_recorded_as_resolved() -> None:
    _, _, reserve, session_id, comment_id = await _setup()

    result = await reserve(comment_id, session_id, _intent())

    assert result is not None
    intent = result.intent
    assert (intent.source, intent.confidence) == (IntentSource.LLM, Decimal("0.85"))
    assert intent.evidence == ("la robe", "M")
    assert intent.quantity == Quantity(1)
    assert intent.outcome is IntentOutcome.RESERVED


async def test_a_parser_match_becomes_a_parser_intent() -> None:
    database, _, reserve, session_id, comment_id = await _setup()
    match = parse_comment("je prends A12 M x2", demo_catalog(), max_quantity=DEMO_MAX_QUANTITY)
    assert isinstance(match, Match)

    result = await reserve(comment_id, session_id, ResolvedIntent.from_match(match))

    assert result is not None
    assert result.intent.source is IntentSource.PARSER
    assert result.intent.confidence == Confidence.EXACT.value
    assert result.intent.evidence == match.evidence
    assert result.intent.quantity == Quantity(2)
    product = database.find_product(session_id, ProductCode("A12"), Variant("M"))
    assert product is not None
    assert product.stock == StockLevel(10, reserved=2)


async def test_short_stock_opens_no_order() -> None:
    _, _, reserve, session_id, comment_id = await _setup(stock_total=1)

    result = await reserve(comment_id, session_id, _intent(quantity=2))

    assert result is not None
    assert result.intent.outcome is IntentOutcome.OUT_OF_STOCK
    assert result.order is None


async def test_a_comment_settled_by_another_delivery_gives_nothing() -> None:
    _, _, reserve, session_id, comment_id = await _setup()
    await reserve(comment_id, session_id, _intent())

    assert await reserve(comment_id, session_id, _intent()) is None


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_the_ttl_must_be_positive(ttl: timedelta) -> None:
    with pytest.raises(ValueError, match="positive"):
        ReserveStock(InMemoryDatabase().orders, FakeClock(), reservation_ttl=ttl)


def test_a_resolved_intent_has_a_confidence_from_0_to_1() -> None:
    for confidence in (Decimal("-0.01"), Decimal("1.01")):
        with pytest.raises(ValueError, match="confidence"):
            ResolvedIntent(
                ProductCode("A12"),
                Variant("M"),
                Quantity(1),
                IntentSource.LLM,
                confidence,
                (),
            )
