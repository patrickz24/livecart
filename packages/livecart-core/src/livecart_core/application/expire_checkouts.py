"""``ExpireCheckouts``: the due orders with a payment in flight, expired at the provider first.

Step 14 of the plan's journey, its second and third phases; the first is
``ExpireReservations``, which leaves alone every order with a pending
attempt. Such an order's stock is released only once the provider has
expired the attempt's session (expire-first): a buyer who paid first is
never left with a released order. Each worker runs it every 5 s, after the
first phase:

1. a lease (``CheckoutExpiry.lease_due_checkouts``): at most 20 orders past
   ``reserved_until`` whose attempt is pending, leased for 30 s, ``FOR
   UPDATE SKIP LOCKED``, so that one sweep alone asks the provider about
   each; the unit commits before any call;
2. for each, at most 4 at a time per worker (``Semaphore(4)``) and holding
   no database connection during the calls: an attempt left without a
   session (a crash, or a provider down, between the two steps of
   ``StartCheckout``) gets it again first, created under the same
   idempotency key and the same URLs (``CheckoutPages``, from
   ``PUBLIC_API_BASE_URL`` as in the API), so the provider answers the same
   session; then ``expire_checkout``:

   - it expired the session: ``CheckoutExpiry.release_checkout`` expires the
     attempt and the order and releases the stock, as the first phase does;
   - it refused: the session is read back. ``expired`` releases the same
     way; ``complete`` and paid is reconciled through the registry's own
     path: the ``checkout.session.completed`` event the provider sends
     (``reconciled:<session>``), recorded in ``processed_webhook_events`` and
     applied by ``ApplyPaymentEvent``, whose compare-and-sets make it count
     once, however the provider's own webhook races it. The order is paid
     and its reserved units sold, never released;
   - anything else (the provider down, a session still open or complete but
     not paid yet) is left as it is: the sweep that takes the order once
     its lease has ended asks again.

Every database unit of the tick runs one at a time, so that the sweep holds
one connection at most, like every background loop of the worker, and none
during a call to the provider. A tick leases again while its claims come
back full, within its budget, and each claim's work ends before its lease
does, so that no other sweep can take an order while this one handles it.
A failure that needs a person (a session the provider does not know, an
attempt of another provider, a key the provider refuses with other
parameters) is ``failed``, logged by the worker at ERROR, and asked again
after the lease like any other.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from livecart_core.application.apply_payment_events import ApplyPaymentEvent, HandledPaymentEvent
from livecart_core.application.checkout import CheckoutPages
from livecart_core.application.dto import (
    CheckoutEventType,
    CheckoutPaymentStatus,
    CheckoutSession,
    CheckoutStatus,
    DueCheckout,
    OrderRecord,
    PaymentEvent,
    PaymentProviderName,
    RegisteredEvent,
    ReleasedCheckout,
    SessionEvent,
)
from livecart_core.application.ports import (
    CHECKOUT_EXPIRY_BATCH,
    CHECKOUT_EXPIRY_LEASE,
    CheckoutExpiry,
    Clock,
    EventPublisher,
    PaymentAttempts,
    PaymentProvider,
    ServiceUnavailable,
    WebhookEventRecorder,
)
from livecart_core.application.session_events import order_updated, stock_updated
from livecart_core.domain.state_machines import OrderStatus

# The calls to the provider one worker makes at once.
CHECKOUT_EXPIRY_SLOTS: Final = 4
# What a claim's work leaves of its lease, at least, before another sweep may take it.
LEASE_MARGIN: Final = timedelta(seconds=5)
# The event id of a session the sweep found paid, in the provider's registry.
RECONCILED_EVENT_PREFIX: Final = "reconciled:"


class CheckoutExpiryOutcome(StrEnum):
    """What asking the provider about one due order came to: the label of the worker's metric."""

    # This sweep expired the session: the attempt and the order expired, the stock released.
    EXPIRED = "expired"
    # The session had expired already: released the same way.
    FOUND_EXPIRED = "found_expired"
    # The buyer paid first: the payment applied as the provider's event would apply it.
    RECONCILED = "reconciled"
    # The provider could not settle it now (down, still open, not paid yet): after the lease.
    RETRY = "retry"
    # What needs a person: logged at ERROR, and asked again after the lease.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HandledCheckout:
    """One due order and what became of it.

    ``order`` is the order as the last unit left it, when one ran, and
    ``order_changed`` whether this sweep moved it; ``recreated`` says that
    the attempt had no session, and got it again under its key. ``error`` is
    what kept a ``retry`` or a ``failed`` order from settling.
    """

    due: DueCheckout
    outcome: CheckoutExpiryOutcome
    order: OrderRecord | None = None
    order_changed: bool = False
    recreated: bool = False
    unpublished_events: int = 0
    error: Exception | None = None

    @property
    def released(self) -> bool:
        """Whether this sweep expired the order and released its reserved units."""
        expired = self.outcome in (
            CheckoutExpiryOutcome.EXPIRED,
            CheckoutExpiryOutcome.FOUND_EXPIRED,
        )
        return expired and self.order_changed

    @property
    def paid(self) -> bool:
        """Whether this sweep's reconciliation paid the order, its reserved units sold."""
        return (
            self.outcome is CheckoutExpiryOutcome.RECONCILED
            and self.order_changed
            and self.order is not None
            and self.order.status is OrderStatus.PAID
        )


@dataclass(frozen=True, slots=True)
class CheckoutExpiryTick:
    """One tick of the later phases: every due order it leased, in order, and its claims."""

    handled: tuple[HandledCheckout, ...] = ()
    claims: int = 0

    def count(self, outcome: CheckoutExpiryOutcome) -> int:
        return sum(1 for checkout in self.handled if checkout.outcome is outcome)


def reconciliation_event(
    provider: PaymentProviderName, session: CheckoutSession, *, now: datetime
) -> PaymentEvent:
    """The ``checkout.session.completed`` event of a session the sweep read back complete.

    The event the provider sends, as the registry keeps it, with an id of its
    own: ``reconciled:<session>``. The amount and the payment status are
    the session's, which ``ApplyPaymentEvent`` checks as it checks the
    provider's.
    """
    return PaymentEvent(
        provider=provider,
        event_id=f"{RECONCILED_EVENT_PREFIX}{session.provider_ref}",
        event_type=CheckoutEventType.COMPLETED.value,
        checkout_ref=session.provider_ref,
        payment_ref=session.payment_ref,
        payment_status=session.payment_status,
        amount_minor=session.amount.amount_minor,
        currency=session.amount.currency,
        occurred_at=now,
    )


class ExpireCheckouts:
    """One tick of the later phases of the expiry sweep; see the module docstring."""

    def __init__(
        self,
        orders: CheckoutExpiry,
        payments: PaymentAttempts,
        provider: PaymentProvider,
        registry: WebhookEventRecorder,
        apply: ApplyPaymentEvent,
        events: EventPublisher,
        clock: Clock,
        *,
        pages: CheckoutPages,
        budget: timedelta,
        batch: int = CHECKOUT_EXPIRY_BATCH,
        lease: timedelta = CHECKOUT_EXPIRY_LEASE,
        slots: int = CHECKOUT_EXPIRY_SLOTS,
    ) -> None:
        if budget <= timedelta(0) or lease <= LEASE_MARGIN:
            raise ValueError(f"a tick needs a time budget, and a lease longer than {LEASE_MARGIN}")
        if batch < 1 or slots < 1:
            raise ValueError("a claim leases one order at least, and asks with one slot at least")
        self._orders = orders
        self._payments = payments
        self._provider = provider
        self._registry = registry
        self._apply = apply
        self._events = events
        self._clock = clock
        self._pages = pages
        self._budget = budget
        self._batch = batch
        self._lease = lease
        self._slots = asyncio.Semaphore(slots)
        # One database unit at a time: the sweep holds one connection at most.
        self._database = asyncio.Lock()

    async def __call__(self) -> CheckoutExpiryTick:
        """Lease and settle until a claim comes back short or the budget is spent.

        A lease that fails raises (``ServiceUnavailable`` when PostgreSQL is
        down): its orders wait for the next tick. Whatever happens to one
        order is its own result, never another's.
        """
        started = self._clock.now()
        handled: list[HandledCheckout] = []
        claims = 0
        while True:
            due = await self._orders.lease_due_checkouts(
                now=self._clock.now(), limit=self._batch, lease=self._lease
            )
            claims += 1
            # The claim's work ends before its lease, by the loop's own clock.
            deadline = (
                asyncio.get_running_loop().time() + (self._lease - LEASE_MARGIN).total_seconds()
            )
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(self._handle(checkout, deadline)) for checkout in due]
            handled.extend(task.result() for task in tasks)
            if len(due) < self._batch or self._clock.now() - started >= self._budget:
                return CheckoutExpiryTick(tuple(handled), claims)

    async def _handle(self, due: DueCheckout, deadline: float) -> HandledCheckout:
        try:
            async with asyncio.timeout_at(deadline):
                return await self._settle(due)
        except (ServiceUnavailable, TimeoutError) as error:
            return HandledCheckout(due, CheckoutExpiryOutcome.RETRY, error=error)
        except Exception as error:
            return HandledCheckout(due, CheckoutExpiryOutcome.FAILED, error=error)

    async def _settle(self, due: DueCheckout) -> HandledCheckout:
        payment = due.payment
        if payment.provider is not self._provider.name:
            raise ValueError(f"the attempt is {payment.provider}'s, not {self._provider.name}'s")
        ref, recreated = payment.provider_ref, False
        if ref is None:
            ref, recreated = await self._recreate(due), True
        async with self._slots:
            expired = await self._provider.expire_checkout(ref)
            session = None if expired else await self._provider.get_checkout(ref)
        if session is None:
            return await self._release(due, CheckoutExpiryOutcome.EXPIRED, recreated)
        if session.status is CheckoutStatus.EXPIRED:
            return await self._release(due, CheckoutExpiryOutcome.FOUND_EXPIRED, recreated)
        if session.status is CheckoutStatus.COMPLETE and (
            session.payment_status is not CheckoutPaymentStatus.UNPAID
        ):
            return await self._reconcile(due, session, recreated)
        # Still open, or complete by a delayed method whose money has not come in.
        return HandledCheckout(due, CheckoutExpiryOutcome.RETRY, recreated=recreated)

    async def _recreate(self, due: DueCheckout) -> str:
        """The session of an attempt left without one, created again under the attempt's key."""
        request = self._pages.request_for(due.order.reference, due.payment)
        async with self._slots:
            session = await self._provider.create_checkout(request)
        recorded = await self._unit(
            lambda: self._payments.record_checkout(
                due.payment.id, provider_ref=session.provider_ref, redirect_url=session.redirect_url
            )
        )
        # The first session recorded stays: a click's meanwhile, under the same key, the same one.
        return recorded.provider_ref or session.provider_ref

    async def _release(
        self, due: DueCheckout, outcome: CheckoutExpiryOutcome, recreated: bool
    ) -> HandledCheckout:
        released = await self._unit(
            lambda: self._orders.release_checkout(
                due.order.id, due.payment.id, now=self._clock.now()
            )
        )
        unpublished = await self._publish(released) if released.order_changed else 0
        return HandledCheckout(
            due, outcome, released.order, released.order_changed, recreated, unpublished
        )

    async def _reconcile(
        self, due: DueCheckout, session: CheckoutSession, recreated: bool
    ) -> HandledCheckout:
        now = self._clock.now()
        event = reconciliation_event(due.payment.provider, session, now=now)
        await self._unit(lambda: self._registry.record(event, now=now))
        try:
            applied: HandledPaymentEvent = await self._unit(
                lambda: self._apply(RegisteredEvent(event, received_at=now))
            )
        except ServiceUnavailable as error:
            # Recorded: the registry's loop applies it, within its 2 s.
            return HandledCheckout(
                due, CheckoutExpiryOutcome.RECONCILED, recreated=recreated, error=error
            )
        return HandledCheckout(
            due,
            CheckoutExpiryOutcome.RECONCILED,
            applied.order,
            applied.order_changed,
            recreated,
            applied.unpublished_events,
        )

    async def _unit[T](self, unit: Callable[[], Awaitable[T]]) -> T:
        """One database unit, started once the one before it has ended."""
        async with self._database:
            return await unit()

    async def _publish(self, released: ReleasedCheckout) -> int:
        """The order expired and its product's counters, best effort; how many failed."""
        order, now = released.order, self._clock.now()
        published: list[SessionEvent] = [order_updated(order, correlation_id=None, occurred_at=now)]
        if released.stock is not None:
            published.append(
                stock_updated(
                    order.session_id,
                    order.product_id,
                    released.stock,
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
