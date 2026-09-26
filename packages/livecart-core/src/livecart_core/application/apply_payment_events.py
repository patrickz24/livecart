"""``ApplyPaymentEvent`` and ``ProcessPaymentEvents``: the registry of payment webhooks, applied.

Step 13 of the plan's journey, its second half. The payment webhook only
records each verified event in ``processed_webhook_events``
(``ReceivePaymentWebhook``); every worker polls that registry every 2 s,
the outbox of the payments (no Redis stream: decision 9 of the plan):

- a claim (``WebhookEventRegistry.claim``) takes at most 20 unprocessed
  events whose lease has ended, oldest first, ``FOR UPDATE SKIP LOCKED``,
  and counts one attempt on each before it is made: ``attempts`` in the
  registry is the one count, a worker that died midway included. Each
  claimed event is leased for 30 s, so no other worker takes it meanwhile,
  and one whose attempt failed is tried again once the lease has ended. An
  event that made its 10 attempts is settled as ``failed``, by the attempt
  that fails the 10th time or, if that worker died, by the next claim;
- ``ApplyPaymentEvent`` applies each of the four ``checkout.session.*``
  events LiveCart acts on, in one unit (``WebhookEventRegistry.settle``)
  that moves the attempt, the order and its stock by compare-and-set and
  marks the event processed: ``completed`` once paid and
  ``async_payment_succeeded`` pay the order (``pending_payment -> paid``, its
  reserved units sold, or ``expired -> paid_after_expiry``, no stock
  effect), ``async_payment_failed`` fails the attempt and ``expired``
  expires it (the sweep releases the order's stock). The amount and the
  currency the provider charged must be the order's: otherwise the event is
  ``failed`` and nothing moves. Any other event, and a session completed
  ``unpaid`` (a delayed method, which an ``async_payment_*`` event settles
  later), is ``ignored``;
- after the commit, the order's new status goes to the session's stream
  (``order.updated``), and the product's counters with it when its units
  were sold (``stock.updated``), best effort, as every event of the
  worker: a ``paid_after_expiry`` order is the dashboard's badge.

The same event delivered again is one row of the registry, applied once; an
event replayed or out of order finds its rows where they are and changes
nothing. A tick claims batch after batch while they come back full, within
its interval, so that a burst drains at once.

The worker logs what needs a person (``failed`` events, payments after
expiry, for a refund by runbook) and counts every result in its metrics.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final

from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    OrderRecord,
    PaymentEvent,
    RegisteredEvent,
    SessionEvent,
    SettlementKind,
    StockLevel,
    WebhookOutcome,
)
from livecart_core.application.ports import (
    Clock,
    EventPublisher,
    ServiceUnavailable,
    WebhookEventRegistry,
)
from livecart_core.application.session_events import order_updated, stock_updated
from livecart_core.domain.payments import PaymentEffect
from livecart_core.domain.state_machines import OrderStatus

# The events one claim takes at most, and the attempts an event gets.
PAYMENT_EVENT_BATCH: Final = 20
PAYMENT_EVENT_MAX_ATTEMPTS: Final = 10
# How long a claimed event stays with its worker: well beyond the few
# milliseconds a batch takes, and the wait before a failed attempt is tried again.
PAYMENT_EVENT_LEASE: Final = timedelta(seconds=30)

# What each of the four events LiveCart acts on does to the money.
EFFECTS: Final = {
    CheckoutEventType.COMPLETED: PaymentEffect.PAID,
    CheckoutEventType.ASYNC_PAYMENT_SUCCEEDED: PaymentEffect.PAID,
    CheckoutEventType.ASYNC_PAYMENT_FAILED: PaymentEffect.FAILED,
    CheckoutEventType.EXPIRED: PaymentEffect.EXPIRED,
}


def effect_of(event: PaymentEvent) -> PaymentEffect | None:
    """What the event does to its session's money, or ``None`` for an event not acted on."""
    try:
        event_type = CheckoutEventType(event.event_type)
    except ValueError:
        return None
    if event_type is CheckoutEventType.COMPLETED and (
        event.payment_status is CheckoutPaymentStatus.UNPAID
    ):
        return None
    return EFFECTS[event_type]


class PaymentEventResult(StrEnum):
    """What handling one claimed event came to: the label of the worker's metric."""

    APPLIED = "applied"
    REPLAYED = "replayed"
    STALE = "stale"
    UNKNOWN_SESSION = "unknown_session"
    NOT_ACTED_ON = "not_acted_on"
    AMOUNT_MISMATCH = "amount_mismatch"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    # Another claim, after this one's lease, applied it first.
    PROCESSED_ELSEWHERE = "processed_elsewhere"
    # The attempt failed; the event is tried again once its lease has ended.
    RETRY = "retry"

    @property
    def outcome(self) -> WebhookOutcome | None:
        """The registry's outcome this result recorded, or ``None`` when it recorded none."""
        return _OUTCOMES.get(self)


_OUTCOMES: Final = {
    PaymentEventResult.APPLIED: WebhookOutcome.APPLIED,
    PaymentEventResult.REPLAYED: WebhookOutcome.IGNORED,
    PaymentEventResult.STALE: WebhookOutcome.IGNORED,
    PaymentEventResult.UNKNOWN_SESSION: WebhookOutcome.IGNORED,
    PaymentEventResult.NOT_ACTED_ON: WebhookOutcome.IGNORED,
    PaymentEventResult.AMOUNT_MISMATCH: WebhookOutcome.FAILED,
    PaymentEventResult.ATTEMPTS_EXHAUSTED: WebhookOutcome.FAILED,
}
_RESULTS: Final = {
    SettlementKind.APPLIED: PaymentEventResult.APPLIED,
    SettlementKind.REPLAYED: PaymentEventResult.REPLAYED,
    SettlementKind.STALE: PaymentEventResult.STALE,
    SettlementKind.UNKNOWN_SESSION: PaymentEventResult.UNKNOWN_SESSION,
    SettlementKind.AMOUNT_MISMATCH: PaymentEventResult.AMOUNT_MISMATCH,
}


@dataclass(frozen=True, slots=True)
class HandledPaymentEvent:
    """One claimed event and what became of it.

    ``order`` is its order as the unit left it, when the event named one;
    ``order_changed`` says whether this event moved it. ``error`` is what
    failed an attempt (``retry``, or the last of ``attempts_exhausted``).
    """

    event: RegisteredEvent
    result: PaymentEventResult
    order: OrderRecord | None = None
    order_changed: bool = False
    unpublished_events: int = 0
    error: Exception | None = None

    @property
    def paid(self) -> bool:
        """Whether this event paid its order, its reserved units sold."""
        return self._moved_to(OrderStatus.PAID)

    @property
    def paid_after_expiry(self) -> bool:
        """Whether this event paid an order whose stock the sweep had released: a refund to make."""
        return self._moved_to(OrderStatus.PAID_AFTER_EXPIRY)

    def _moved_to(self, status: OrderStatus) -> bool:
        return self.order_changed and self.order is not None and self.order.status is status


@dataclass(frozen=True, slots=True)
class PaymentEventsTick:
    """One tick of the registry loop: every event it claimed, in order, and its claims."""

    handled: tuple[HandledPaymentEvent, ...] = ()
    claims: int = 0

    def count(self, result: PaymentEventResult) -> int:
        return sum(1 for event in self.handled if event.result is result)


class ApplyPaymentEvent:
    """Applies one claimed event of the registry; see the module docstring."""

    def __init__(
        self, registry: WebhookEventRegistry, events: EventPublisher, clock: Clock
    ) -> None:
        self._registry = registry
        self._events = events
        self._clock = clock

    async def __call__(self, registered: RegisteredEvent) -> HandledPaymentEvent:
        """Apply the event; ``ServiceUnavailable`` or any error leaves it for another attempt."""
        event = registered.event
        effect = effect_of(event)
        now = self._clock.now()
        if effect is None:
            marked = await self._registry.mark_processed(
                event.provider, event.event_id, WebhookOutcome.IGNORED, now=now
            )
            result = PaymentEventResult.NOT_ACTED_ON
            return HandledPaymentEvent(
                registered, result if marked else PaymentEventResult.PROCESSED_ELSEWHERE
            )
        settlement = await self._registry.settle(event, effect, now=now)
        if settlement is None:
            return HandledPaymentEvent(registered, PaymentEventResult.PROCESSED_ELSEWHERE)
        order = settlement.order
        unpublished = 0
        if settlement.order_changed and order is not None:
            unpublished = await self._publish(order, settlement.stock)
        return HandledPaymentEvent(
            registered,
            _RESULTS[settlement.kind],
            settlement.order,
            settlement.order_changed,
            unpublished,
        )

    async def _publish(self, order: OrderRecord, stock: StockLevel | None) -> int:
        """The order as it stands, and its product's counters when they moved; best effort."""
        now = self._clock.now()
        published: list[SessionEvent] = [order_updated(order, correlation_id=None, occurred_at=now)]
        if stock is not None:
            published.append(
                stock_updated(
                    order.session_id,
                    order.product_id,
                    stock,
                    correlation_id=None,
                    occurred_at=now,
                )
            )
        failed = 0
        for event in published:
            try:
                await self._events.publish(event)
            except ServiceUnavailable:
                failed += 1
        return failed


class ProcessPaymentEvents:
    """One tick of the worker's registry loop; see the module docstring."""

    def __init__(
        self,
        registry: WebhookEventRegistry,
        apply: ApplyPaymentEvent,
        clock: Clock,
        *,
        budget: timedelta,
        batch: int = PAYMENT_EVENT_BATCH,
        lease: timedelta = PAYMENT_EVENT_LEASE,
        max_attempts: int = PAYMENT_EVENT_MAX_ATTEMPTS,
    ) -> None:
        if budget <= timedelta(0) or lease <= timedelta(0):
            raise ValueError("a tick needs a time budget, and its claims a lease")
        if batch < 1 or max_attempts < 1:
            raise ValueError("a claim takes one event at least, of one attempt at least")
        self._registry = registry
        self._apply = apply
        self._clock = clock
        self._budget = budget
        self._batch = batch
        self._lease = lease
        self._max_attempts = max_attempts

    async def __call__(self) -> PaymentEventsTick:
        """Claim and apply until a claim comes back short or the budget is spent.

        A claim that fails raises (``ServiceUnavailable`` when PostgreSQL is
        down): the events it would have taken wait for the next tick.
        """
        started = self._clock.now()
        handled: list[HandledPaymentEvent] = []
        claims = 0
        while True:
            claim = await self._registry.claim(
                now=self._clock.now(),
                limit=self._batch,
                lease=self._lease,
                max_attempts=self._max_attempts,
            )
            claims += 1
            handled.extend(
                HandledPaymentEvent(event, PaymentEventResult.ATTEMPTS_EXHAUSTED)
                for event in claim.exhausted
            )
            for registered in claim.claimed:
                handled.append(await self._attempt(registered))
            taken = len(claim.claimed) + len(claim.exhausted)
            if taken < self._batch or self._clock.now() - started >= self._budget:
                return PaymentEventsTick(tuple(handled), claims)

    async def _attempt(self, registered: RegisteredEvent) -> HandledPaymentEvent:
        """One attempt, already counted by the claim; its last failure settles the event."""
        try:
            return await self._apply(registered)
        except Exception as error:
            # Whatever failed, the claim counted the attempt: the event is
            # tried again once its lease has ended, up to max_attempts.
            if registered.attempts < self._max_attempts:
                return HandledPaymentEvent(registered, PaymentEventResult.RETRY, error=error)
            return await self._give_up(registered, error)

    async def _give_up(self, registered: RegisteredEvent, error: Exception) -> HandledPaymentEvent:
        event = registered.event
        try:
            marked = await self._registry.mark_processed(
                event.provider, event.event_id, WebhookOutcome.FAILED, now=self._clock.now()
            )
        except ServiceUnavailable:
            # The next claim, once the lease has ended, settles it as failed.
            return HandledPaymentEvent(registered, PaymentEventResult.RETRY, error=error)
        result = (
            PaymentEventResult.ATTEMPTS_EXHAUSTED
            if marked
            else PaymentEventResult.PROCESSED_ELSEWHERE
        )
        return HandledPaymentEvent(registered, result, error=error)
