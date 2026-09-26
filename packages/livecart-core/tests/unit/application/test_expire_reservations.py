from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    ExpiredReservations,
    IntentSource,
    ReservationRequest,
    SessionEventType,
    StockLevel,
)
from livecart_core.application.expire_reservations import ExpireReservations, SweepResult
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import OrderStatus
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import (
    FakeClock,
    FakeEventPublisher,
    InMemoryDatabase,
    InMemoryOrderRepository,
)

TTL = timedelta(seconds=900)
BUDGET = timedelta(seconds=5)


class World:
    def __init__(self, orders: InMemoryOrderRepository | None = None, *, batch: int = 100) -> None:
        self.clock = FakeClock()
        self.database = InMemoryDatabase()
        self.events = FakeEventPublisher()
        self.session = self.database.add_live_session()
        self.b07 = self.database.add_product(self.session.id, "B07", stock_total=100).id
        self.a12 = self.database.add_product(self.session.id, "A12", stock_total=100).id
        self.expire = ExpireReservations(
            orders or self.database.orders, self.events, self.clock, budget=BUDGET, batch=batch
        )

    async def order(self, comment: str, code: str = "B07") -> UUID:
        envelope = queued_comment(external_comment_id=comment, author=f"buyer-of-{comment}")
        claim = await self.database.comments.claim(
            envelope, holder="worker-1/1-0", now=self.clock.now(), lease=timedelta(seconds=30)
        )
        result = await self.database.orders.reserve(
            ReservationRequest(
                comment_id=claim.comment.id,
                session_id=self.session.id,
                product_code=ProductCode(code),
                variant=Variant(),
                quantity=Quantity(2),
                source=IntentSource.PARSER,
                confidence=Decimal("1.00"),
                evidence=("je prends", code),
                now=self.clock.now(),
                reserved_until=self.clock.now() + TTL,
            )
        )
        assert result is not None
        assert result.order is not None
        return result.order.id

    def stock(self, product_id: UUID) -> StockLevel:
        return self.database.tables.products[product_id].stock


async def test_nothing_due_expires_nothing_and_publishes_nothing() -> None:
    world = World()
    await world.order("c-1")

    result = await world.expire()

    assert result == SweepResult(expired=0, batches=1)
    assert world.events.published == []


async def test_due_orders_expire_their_stock_returns_and_their_events_are_published() -> None:
    world = World()
    orders = [await world.order("c-1"), await world.order("c-2"), await world.order("c-3", "A12")]
    world.clock.advance(TTL + timedelta(seconds=1))

    result = await world.expire()

    assert result == SweepResult(expired=3, batches=1)
    assert {world.database.tables.orders[order].status for order in orders} == {OrderStatus.EXPIRED}
    assert (world.stock(world.b07), world.stock(world.a12)) == (StockLevel(100),) * 2
    types = [event.type for event in world.events.published]
    assert types == [SessionEventType.ORDER_UPDATED] * 3 + [SessionEventType.STOCK_UPDATED] * 2
    assert {event.correlation_id for event in world.events.published} == {None}
    assert {event.session_id for event in world.events.published} == {world.session.id}
    stock_events = world.events.of_type(SessionEventType.STOCK_UPDATED)
    assert [event.data["product_id"] for event in stock_events] == [
        str(world.b07),
        str(world.a12),
    ]


async def test_the_sweep_goes_on_while_its_batches_come_back_full() -> None:
    world = World(batch=2)
    for n in range(5):
        await world.order(f"c-{n}")
    world.clock.advance(TTL + timedelta(seconds=1))

    result = await world.expire()

    assert result == SweepResult(expired=5, batches=3)
    assert world.stock(world.b07) == StockLevel(100)


async def test_an_exact_last_batch_is_followed_by_an_empty_one() -> None:
    world = World(batch=2)
    for n in range(4):
        await world.order(f"c-{n}")
    world.clock.advance(TTL + timedelta(seconds=1))

    assert await world.expire() == SweepResult(expired=4, batches=3)


class _SlowOrders(InMemoryOrderRepository):
    """Each batch takes 2 s of the clock's time."""

    def __init__(self, database: InMemoryDatabase, clock: FakeClock) -> None:
        super().__init__(database)
        self._clock = clock

    async def expire_due(self, *, now: datetime, limit: int) -> ExpiredReservations:
        expired = await super().expire_due(now=now, limit=limit)
        self._clock.advance(2)
        return expired


async def test_the_sweep_stops_at_the_end_of_its_budget() -> None:
    world = World(batch=1)
    world.expire = ExpireReservations(
        _SlowOrders(world.database, world.clock), world.events, world.clock, budget=BUDGET, batch=1
    )
    for n in range(10):
        await world.order(f"c-{n}")
    world.clock.advance(TTL + timedelta(seconds=1))

    result = await world.expire()

    # Batches end at 2, 4 then 6 s: the third one passes the 5 s budget.
    assert result == SweepResult(expired=3, batches=3)
    assert await world.expire() == SweepResult(expired=3, batches=3)


async def test_events_that_fail_to_publish_are_counted() -> None:
    world = World()
    await world.order("c-1")
    world.clock.advance(TTL + timedelta(seconds=1))
    world.events.fail_next(1)

    result = await world.expire()

    assert result == SweepResult(expired=1, batches=1, unpublished_events=1)
    assert [event.type for event in world.events.published] == [SessionEventType.STOCK_UPDATED]


class _UnreachableOrders(InMemoryOrderRepository):
    async def expire_due(self, *, now: datetime, limit: int) -> ExpiredReservations:
        raise ServiceUnavailable("database unavailable: scripted")


async def test_an_unavailable_database_reaches_the_loop() -> None:
    world = World()
    world.expire = ExpireReservations(
        _UnreachableOrders(world.database), world.events, world.clock, budget=BUDGET
    )

    with pytest.raises(ServiceUnavailable):
        await world.expire()


@pytest.mark.parametrize(("budget", "batch"), [(timedelta(0), 100), (BUDGET, 0)])
def test_a_sweep_needs_a_budget_and_a_batch(budget: timedelta, batch: int) -> None:
    world = World()

    with pytest.raises(ValueError, match="time budget"):
        ExpireReservations(
            world.database.orders, world.events, world.clock, budget=budget, batch=batch
        )


def test_an_expired_batch_names_the_stock_of_each_product_it_released() -> None:
    with pytest.raises(ValueError, match="every product released"):
        ExpiredReservations((), {UUID(int=1): StockLevel(1)})
