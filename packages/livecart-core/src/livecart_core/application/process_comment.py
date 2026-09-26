"""``ProcessComment``: one delivery of a queued comment, from its lease to its acknowledgment.

Steps 5 to 12 of the plan's journey of a comment. Each step is a unit of its
own, so that no connection is ever held across another port:

1. **Unit 1, the lease** (``CommentInbox.claim``): the comment is recorded if
   it is new, and this delivery takes its lease until ``now + lease``, the
   message deadline (``MESSAGE_PROCESS_TIMEOUT_S``), so that the lease
   outlives the handler that the worker cancels at that deadline. The lease
   belongs to this delivery (``lease_holder``): a retry of it takes the lease
   again, any other delivery of the comment finds it running. When the lease
   is not taken:

   - the comment is settled already: its stored outcome is published again,
     its buyer notified if no notice went out yet (below), then the delivery
     is acknowledged, since the platform or the queue only delivered it twice;
   - another delivery holds the lease: this one is acknowledged at once,
     because the holder's own entry stays pending in the queue, and is
     reclaimed if its worker dies.

2. **Session resolution** by (platform, external live id): no session is a
   ``session_not_found`` rejection, a session that is not live a
   ``session_not_live`` one. The session's catalog is loaded next.
3. **The deterministic parser** reads the comment's stored text (the first
   delivery's): a ``Rejected`` result settles the comment with its reason,
   and a ``Match`` goes to unit 3. An ``Ambiguous`` one goes to
4. **the LLM fallback** (``IntentFallback``), outside any transaction, and
   only when the comment has no receipt yet: a receipt that an earlier
   delivery stored is decided on again, never paid for twice. The fallback
   gets what is left of the message deadline, less ``db_budget``, the time
   kept for the units that follow; with none left, the receipt is a
   ``timeout`` without a call. **Unit 2** stores the receipt unless the
   comment has one (``CommentInbox.record_llm_receipt``), and the decision
   is made on the receipt stored, read back, not on the one received:
   ``decide_from_llm`` checks its proposal against the catalog and the
   quantity bound. An intent it accepts goes to unit 3, with the source
   ``llm``; anything else (the fallback disabled, busy, over budget or
   failed, or a reading the domain refuses) settles the comment as
   ``ambiguous_unresolved``.
5. **Unit 3** (``ReserveStock``), one transaction that settles the comment as
   processed, reserves the stock, records the intent and opens the order;
   every deadline comes from the ``Clock`` port.
6. **After the commit**, the session events are published, best effort (a
   failure is counted in the result, never raised: the dashboard catches up
   from its next snapshot). The buyer of an order opened is then notified
   (``BuyerNotifier``) of its reference and its access token, the token
   computed again from the order id (``livecart_core.order_tokens``): nothing
   stores it in clear. ``orders.buyer_notified_at`` is stamped after the
   notice, and the delivery is acknowledged last.

A crash between the commit and the acknowledgment leaves the delivery
pending, and its redelivery is a duplicate: it sends the notice again, with
the very same token, when ``buyer_notified_at`` is still empty and the order
still pending, and does not otherwise. A buyer may get the same message
twice (the crash fell between the notice and its stamp), never two tokens.

Every settlement carries the guard ``status = 'received'``: a delivery that
finds the comment settled by another one in the meantime handles it as a
duplicate. ``ServiceUnavailable`` from any unit reaches the caller with the
delivery unacknowledged: the worker retries it in the process, then leaves it
to the reclaim loop, and whatever committed before the failure makes the
retry a duplicate rather than a second effect.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from livecart_core.application.dto import (
    MAX_IDENTIFIER_LENGTH,
    BuyerNotification,
    FallbackReceipt,
    FallbackRequest,
    InboxComment,
    OrderRecord,
    ParseSummary,
    QueueDelivery,
    ReservationResult,
    ResolvedIntent,
    SessionEvent,
)
from livecart_core.application.ports import (
    BuyerNotifier,
    Clock,
    CommentInbox,
    CommentQueue,
    EventPublisher,
    IntentFallback,
    LiveSessionRepository,
    OrderReservations,
    RecordNotFound,
    ServiceUnavailable,
)
from livecart_core.application.reserve_stock import ReserveStock
from livecart_core.application.session_events import comment_rejected, reservation_events
from livecart_core.domain.catalog import Catalog
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.llm_decision import decide_from_llm
from livecart_core.domain.outcomes import LlmOutcome, RejectionReason
from livecart_core.domain.parser import Ambiguous, Rejected, parse_comment
from livecart_core.domain.quantity import QUANTITY_CEILING
from livecart_core.domain.state_machines import (
    CommentStatus,
    LiveSessionStatus,
    OrderStatus,
    TransitionOutcome,
)

# An order id -> its access token: OrderTokens.token_for in production.
type TokenFor = Callable[[UUID], str]

# Between the consumer's name and the delivery id in a lease holder.
HOLDER_SEPARATOR: Final = "/"


def lease_holder(consumer: str, delivery_id: str) -> str:
    """Who holds a lease: one delivery of one consumer, as ``comments.claimed_by`` stores it."""
    holder = f"{consumer}{HOLDER_SEPARATOR}{delivery_id}"
    if not consumer or len(holder) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"a lease holder has 1 to {MAX_IDENTIFIER_LENGTH} characters, "
            f"consumer and delivery id included, not {len(holder)}"
        )
    return holder


class Handling(StrEnum):
    """What one delivery did with its comment."""

    # This delivery settled the comment through unit 3: an intent was recorded.
    PROCESSED = "processed"
    # This delivery settled the comment as rejected.
    REJECTED = "rejected"
    # The comment was settled already: its stored outcome was published again.
    DUPLICATE = "duplicate"
    # Another delivery holds the comment's lease.
    IN_FLIGHT = "in_flight"


@dataclass(frozen=True, slots=True)
class HandledComment:
    """The delivery's result, acknowledged: what the worker logs and counts.

    ``comment`` is the inbox row as this delivery found it; ``reservation``
    what unit 3 recorded, by this delivery or, for a duplicate, by the one
    that settled it; ``rejection`` the reason of a rejected comment.
    """

    handling: Handling
    comment: InboxComment
    reservation: ReservationResult | None = None
    rejection: RejectionReason | None = None
    # Events the publisher failed to append (best effort: never retried).
    unpublished_events: int = 0
    # Whether this delivery sent the buyer the order's reference and token.
    notified: bool = False
    # The outcome of the fallback's receipt the comment was decided on, when
    # the parser left it ambiguous: this delivery's, or an earlier one's.
    llm_outcome: LlmOutcome | None = None


class ProcessComment:
    """One delivery of a queued comment; see the module docstring."""

    def __init__(
        self,
        *,
        inbox: CommentInbox,
        sessions: LiveSessionRepository,
        orders: OrderReservations,
        queue: CommentQueue,
        events: EventPublisher,
        notifier: BuyerNotifier,
        access_token: TokenFor,
        fallback: IntentFallback,
        clock: Clock,
        lease: timedelta,
        db_budget: timedelta,
        reservation_ttl: timedelta,
        max_quantity: int,
    ) -> None:
        if lease <= timedelta(0):
            raise ValueError(f"lease must be positive, not {lease}")
        if db_budget <= timedelta(0):
            raise ValueError(f"db_budget must be positive, not {db_budget}")
        if not 1 <= max_quantity <= QUANTITY_CEILING:
            raise ValueError(f"max_quantity must be from 1 to {QUANTITY_CEILING}")
        self._inbox = inbox
        self._sessions = sessions
        self._orders = orders
        self._queue = queue
        self._events = events
        self._notifier = notifier
        self._access_token = access_token
        self._fallback = fallback
        self._clock = clock
        self._lease = lease
        self._db_budget_s = db_budget.total_seconds()
        self._max_quantity = max_quantity
        self._reserve = ReserveStock(orders, clock, reservation_ttl=reservation_ttl)

    async def __call__(
        self, delivery: QueueDelivery, *, consumer: str, deadline: float | None = None
    ) -> HandledComment:
        """Handle one delivery for ``consumer``, the queue consumer that received it.

        ``deadline`` is when the consumer cancels the handler, on the event
        loop's clock (``loop.time()``, what ``asyncio.timeout_at`` reads): the
        fallback's time is counted from it. ``None`` is a lease from now.
        """
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + self._lease.total_seconds()
        envelope = delivery.comment
        claim = await self._inbox.claim(
            envelope,
            holder=lease_holder(consumer, delivery.delivery_id),
            now=self._clock.now(),
            lease=self._lease,
        )
        comment = claim.comment
        if not claim.acquired:
            if comment.status is CommentStatus.RECEIVED:
                await self._queue.ack(delivery)
                return HandledComment(Handling.IN_FLIGHT, comment)
            return await self._duplicate(delivery, comment)

        incoming = envelope.comment
        session = await self._sessions.find_by_live_id(incoming.platform, incoming.external_live_id)
        if session is None:
            return await self._reject(
                delivery, comment, RejectionReason.SESSION_NOT_FOUND, session_id=None
            )
        if session.status is not LiveSessionStatus.LIVE:
            return await self._reject(
                delivery, comment, RejectionReason.SESSION_NOT_LIVE, session_id=session.id
            )

        catalog = await self._sessions.load_catalog(session.id)
        parsed = parse_comment(comment.text, catalog, max_quantity=self._max_quantity)
        parse = ParseSummary.of(parsed)
        if isinstance(parsed, Rejected):
            return await self._reject(
                delivery, comment, parsed.reason, session_id=session.id, parse=parse
            )
        llm_outcome = None
        if isinstance(parsed, Ambiguous):
            receipt = await self._receipt(comment, session.id, parsed, catalog, deadline=deadline)
            llm_outcome = receipt.outcome
            choice = None
            if receipt.proposal is not None:
                choice = decide_from_llm(receipt.proposal, catalog, max_quantity=self._max_quantity)
            if choice is None:
                return await self._reject(
                    delivery,
                    comment,
                    RejectionReason.AMBIGUOUS_UNRESOLVED,
                    session_id=session.id,
                    parse=parse,
                    llm_outcome=llm_outcome,
                )
            intent = ResolvedIntent.from_llm(choice, evidence=parsed.evidence)
        else:
            intent = ResolvedIntent.from_match(parsed)

        result = await self._reserve(comment.id, session.id, intent, parse=parse)
        if result is None:
            return await self._duplicate(delivery, await self._reread(comment.id))
        unpublished = await self._publish(
            reservation_events(
                result, correlation_id=comment.correlation_id, occurred_at=self._clock.now()
            )
        )
        notified = result.order is not None and await self._notify(comment, result.order)
        await self._queue.ack(delivery)
        return HandledComment(
            Handling.PROCESSED,
            comment,
            reservation=result,
            unpublished_events=unpublished,
            notified=notified,
            llm_outcome=llm_outcome,
        )

    async def _receipt(
        self,
        comment: InboxComment,
        session_id: UUID,
        ambiguity: Ambiguous,
        catalog: Catalog,
        *,
        deadline: float,
    ) -> FallbackReceipt:
        """The comment's receipt: an earlier delivery's, or one attempt now, stored by unit 2."""
        if comment.llm_receipt is not None:
            return comment.llm_receipt
        left = deadline - asyncio.get_running_loop().time() - self._db_budget_s
        if left > 0:
            attempt = await self._fallback.resolve(
                FallbackRequest(
                    session_id=session_id,
                    text=comment.text,
                    ambiguity=ambiguity,
                    catalog=catalog,
                    timeout_seconds=left,
                )
            )
        else:
            # What came before took the fallback's time: no call to start now.
            attempt = FallbackReceipt(LlmOutcome.TIMEOUT)
        return await self._inbox.record_llm_receipt(comment.id, attempt)

    async def _reject(
        self,
        delivery: QueueDelivery,
        comment: InboxComment,
        reason: RejectionReason,
        *,
        session_id: UUID | None,
        parse: ParseSummary | None = None,
        llm_outcome: LlmOutcome | None = None,
    ) -> HandledComment:
        try:
            outcome = await self._inbox.reject(
                comment.id, reason, session_id=session_id, parse=parse
            )
        except InvalidTransition:
            # Settled otherwise by another delivery since this one took the lease.
            outcome = TransitionOutcome.NOOP
        if outcome is TransitionOutcome.NOOP:
            return await self._duplicate(delivery, await self._reread(comment.id))
        events = []
        # A comment without a session has no stream to be published to.
        if session_id is not None:
            events.append(
                comment_rejected(
                    session_id,
                    comment.id,
                    reason,
                    correlation_id=comment.correlation_id,
                    occurred_at=self._clock.now(),
                )
            )
        unpublished = await self._publish(events)
        await self._queue.ack(delivery)
        return HandledComment(
            Handling.REJECTED,
            comment,
            rejection=reason,
            unpublished_events=unpublished,
            llm_outcome=llm_outcome,
        )

    async def _duplicate(self, delivery: QueueDelivery, comment: InboxComment) -> HandledComment:
        """A settled comment delivered again: its stored outcome is published again."""
        now = self._clock.now()
        reservation = None
        order = None
        events: list[SessionEvent] = []
        if comment.status is CommentStatus.PROCESSED:
            reservation = await self._orders.find_reservation(comment.id)
            if reservation is not None:
                order = reservation.order
                events = reservation_events(
                    reservation, correlation_id=comment.correlation_id, occurred_at=now
                )
        elif comment.rejection_reason is not None and comment.session_id is not None:
            events = [
                comment_rejected(
                    comment.session_id,
                    comment.id,
                    comment.rejection_reason,
                    correlation_id=comment.correlation_id,
                    occurred_at=now,
                )
            ]
        unpublished = await self._publish(events)
        # The notice of an earlier delivery that died before stamping it; an
        # order no longer pending is past what its buyer could do with it.
        notified = (
            order is not None
            and order.buyer_notified_at is None
            and order.status is OrderStatus.PENDING_PAYMENT
            and await self._notify(comment, order)
        )
        await self._queue.ack(delivery)
        return HandledComment(
            Handling.DUPLICATE,
            comment,
            reservation=reservation,
            rejection=comment.rejection_reason,
            unpublished_events=unpublished,
            notified=notified,
            llm_outcome=None if comment.llm_receipt is None else comment.llm_receipt.outcome,
        )

    async def _notify(self, comment: InboxComment, order: OrderRecord) -> bool:
        """Send the buyer the order's reference and token, then stamp the notice."""
        await self._notifier.notify(
            BuyerNotification(
                order_id=order.id,
                platform=comment.platform,
                recipient=order.buyer_platform_user_id,
                reference=order.reference,
                access_token=self._access_token(order.id),
            )
        )
        await self._orders.mark_buyer_notified(order.id, now=self._clock.now())
        return True

    async def _reread(self, comment_id: UUID) -> InboxComment:
        comment = await self._inbox.get(comment_id)
        if comment is None:
            raise RecordNotFound(f"no comment {comment_id}")
        return comment

    async def _publish(self, events: Sequence[SessionEvent]) -> int:
        """Publish each event, best effort; return how many failed."""
        failed = 0
        for event in events:
            try:
                await self._events.publish(event)
            except ServiceUnavailable:
                failed += 1
        return failed
