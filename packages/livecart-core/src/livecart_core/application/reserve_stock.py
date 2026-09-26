"""``ReserveStock``: unit 3 of a comment, the reservation of one resolved intent.

The comment is settled as processed, the stock reserved, the intent recorded
and a pending order opened, all in the one transaction of
``OrderReservations.reserve``: on PostgreSQL a single conditional ``UPDATE``
reserves the stock, so two comments racing for the last unit can never both
win it. The use case owns what that unit must not decide for itself: the
time, from the ``Clock`` port, and how long an unpaid reservation holds the
stock, ``RESERVATION_TTL_SECONDS``.

``ProcessComment`` (milestone 4a) calls it once the comment's lease is held,
its session resolved and its intent settled by the parser or the LLM.
"""

from datetime import timedelta
from uuid import UUID

from livecart_core.application.dto import (
    ParseSummary,
    ReservationRequest,
    ReservationResult,
    ResolvedIntent,
)
from livecart_core.application.ports import Clock, OrderReservations


class ReserveStock:
    """Reserve one resolved intent and open its order, or record why it could not be.

    The result is ``None`` when another delivery of the comment settled it
    first: nothing was written, and the caller acknowledges the delivery.
    ``ServiceUnavailable`` (the database unavailable, a lock that timed out,
    three colliding order references) leaves the comment received, so that
    its redelivery tries again.
    """

    def __init__(
        self, orders: OrderReservations, clock: Clock, *, reservation_ttl: timedelta
    ) -> None:
        if reservation_ttl <= timedelta(0):
            raise ValueError(f"reservation_ttl must be positive, not {reservation_ttl}")
        self._orders = orders
        self._clock = clock
        self._reservation_ttl = reservation_ttl

    async def __call__(
        self,
        comment_id: UUID,
        session_id: UUID,
        intent: ResolvedIntent,
        *,
        parse: ParseSummary | None = None,
    ) -> ReservationResult | None:
        """Unit 3; ``parse``, what the parser said of the comment, is recorded with it."""
        now = self._clock.now()
        return await self._orders.reserve(
            ReservationRequest(
                comment_id=comment_id,
                session_id=session_id,
                product_code=intent.product_code,
                variant=intent.variant,
                quantity=intent.quantity,
                source=intent.source,
                confidence=intent.confidence,
                evidence=intent.evidence,
                now=now,
                reserved_until=now + self._reservation_ttl,
                parse=parse,
            )
        )
