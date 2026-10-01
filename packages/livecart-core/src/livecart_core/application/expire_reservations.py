"""``ExpireReservations``: every 5 s, the stock of the reservations nobody paid, released.

Step 14 of the plan's journey, its first phase: the orders without a payment
in flight (those with one are ``ExpireCheckouts``', which asks the provider
to expire the attempt's session first). Each worker runs it every 5 s:

- a batch is one unit (``ReservationExpiry.expire_due``): at most
  ``EXPIRY_BATCH`` (100) orders past ``reserved_until`` at ``now``, the
  ``Clock``'s, expired and their units released. Sweeps running at once in
  several workers skip each other's orders (``FOR UPDATE SKIP LOCKED``), so
  each order is expired, and its stock released, exactly once;
- a full batch means more may be due: the sweep goes on, batch after batch,
  until one comes back short or the tick's budget (its interval) runs out,
  so that a sweep never overlaps the next tick of its own loop;
- after each batch's commit, the events of what it changed are published,
  best effort: every order expired and the counters of every product
  released.
"""

from dataclasses import dataclass
from datetime import timedelta

from livecart_core.application.dto import ExpiredReservations, SessionEvent
from livecart_core.application.ports import (
    EXPIRY_BATCH,
    Clock,
    EventPublisher,
    ReservationExpiry,
    ServiceUnavailable,
)
from livecart_core.application.session_events import order_updated, stock_updated


@dataclass(frozen=True, slots=True)
class SweepResult:
    """One tick of the sweep: how many orders it expired, in how many batches."""

    expired: int = 0
    batches: int = 0
    unpublished_events: int = 0


class ExpireReservations:
    """One tick of the expiry sweep; see the module docstring."""

    def __init__(
        self,
        orders: ReservationExpiry,
        events: EventPublisher,
        clock: Clock,
        *,
        budget: timedelta,
        batch: int = EXPIRY_BATCH,
    ) -> None:
        if budget <= timedelta(0) or batch < 1:
            raise ValueError("a sweep needs a time budget and batches of one order at least")
        self._orders = orders
        self._events = events
        self._clock = clock
        self._budget = budget
        self._batch = batch

    async def __call__(self) -> SweepResult:
        started = self._clock.now()
        expired = batches = unpublished = 0
        while True:
            now = self._clock.now()
            released = await self._orders.expire_due(now=now, limit=self._batch)
            batches += 1
            expired += len(released.orders)
            unpublished += await self._publish(released)
            if len(released.orders) < self._batch or self._clock.now() - started >= self._budget:
                return SweepResult(expired, batches, unpublished)

    async def _publish(self, released: ExpiredReservations) -> int:
        """The events of one batch, best effort; return how many failed."""
        now = self._clock.now()
        events: list[SessionEvent] = [
            order_updated(order, correlation_id=None, occurred_at=now) for order in released.orders
        ]
        sessions = {order.product_id: order.session_id for order in released.orders}
        events.extend(
            stock_updated(
                sessions[product_id], product_id, stock, correlation_id=None, occurred_at=now
            )
            for product_id, stock in released.stock.items()
        )
        failed = 0
        for event in events:
            try:
                await self._events.publish(event)
            except ServiceUnavailable:
                failed += 1
        return failed
