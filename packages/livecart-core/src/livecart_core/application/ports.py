"""The ports of the application layer: what a use case needs from the outside world.

Each port is a ``typing.Protocol``: an adapter satisfies it by its shape, with
no base class to inherit, and mypy checks the fit wherever an adapter is
passed as a port. The entry points (``livecart_api``, ``livecart_worker``)
inject the real adapters, the tests the fakes of ``livecart_core.testing.fakes``;
a use case only ever sees the port.

The docstring of each port states what every implementation guarantees, and
the fakes do exactly that: a use case that relies on anything more relies on
an accident of one adapter. Rules shared by every port:

- a repository method is one atomic unit: the SQL adapters run it in one
  transaction, the in-memory fakes without an ``await`` inside. A use case
  therefore never holds a transaction or a connection across a call to
  another port, and a network call (the LLM, a payment provider, a carrier)
  always falls between two units, never inside one;
- a status change is a compare-and-set against the transition table of its
  machine (``livecart_core.domain.state_machines``): it returns ``APPLIED``,
  or ``NOOP`` when the row already holds the target, and raises
  ``InvalidTransition`` otherwise;
- an identifier that names no row raises ``RecordNotFound``, and a failure
  worth retrying later (a server down, a timeout) raises ``ServiceUnavailable``;
- every ``now`` a use case passes comes from the ``Clock`` port, so that a
  test fixes time.

The ports hold what the plan's journeys of a comment, a payment and a
shipment name. The milestone that implements a port may refine it, together
with its fake, in the same commit.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from livecart_core.application.dto import (
    AttemptOpening,
    BuyerNotification,
    CarrierName,
    CheckoutRequest,
    CheckoutSession,
    CommentClaim,
    FallbackReceipt,
    FallbackRequest,
    InboxComment,
    IncomingComment,
    LiveSessionRecord,
    OrderRecord,
    OrderTransition,
    PaymentEvent,
    PaymentProviderName,
    PaymentRecord,
    Platform,
    ProductRecord,
    QueuedComment,
    QueueDelivery,
    RegisteredEvent,
    ReservationRequest,
    ReservationResult,
    SessionEvent,
    ShipmentRecord,
    ShipmentRef,
    ShipmentRequest,
    ShippingAddress,
    TrackingUpdate,
    WebhookOutcome,
)
from livecart_core.domain.catalog import Catalog
from livecart_core.domain.outcomes import LlmOutcome, RejectionReason
from livecart_core.domain.reference import OrderReference
from livecart_core.domain.state_machines import (
    LiveSessionStatus,
    PaymentStatus,
    ShipmentStatus,
    TransitionOutcome,
)

# --- Errors ----------------------------------------------------------------------------


class PortError(Exception):
    """Base of the errors a port raises, so that a caller can catch the whole family."""


class RecordNotFound(PortError, LookupError):
    """An identifier that names no row."""


class ServiceUnavailable(PortError):
    """A transient failure (a server down, a timeout, retries used up): try again later."""


class InvalidSignature(PortError):
    """A webhook whose signature does not verify: a generic 401, and the body is read no further."""


class StockConflict(PortError):
    """A restock that would bring a product's stock total below its reserved and sold units."""


# --- Time ------------------------------------------------------------------------------


class Clock(Protocol):
    """The current time: the system clock in production, a settable one in tests."""

    def now(self) -> datetime:
        """A timezone-aware UTC datetime: every deadline and lease end is computed from it."""


# --- Comment ingestion -----------------------------------------------------------------


class PlatformAdapter(Protocol):
    """The entry of one platform's comment webhook: check the signature, then parse.

    ``verify`` runs first, on the raw body, before anything is parsed, queued
    or written. ``parse`` then returns the comments in the common format.
    Both only compute, so they are synchronous. The Facebook, Instagram and
    TikTok adapters are stubs that refuse to start when enabled.
    """

    @property
    def platform(self) -> Platform:
        """The platform of the webhook path this adapter serves."""

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        """Raise ``InvalidSignature`` unless the headers sign this exact body."""

    def parse(self, raw_body: bytes) -> list[IncomingComment]:
        """The comments of a verified body.

        A body that is no batch raises ``ValueError`` (pydantic's
        ``ValidationError`` is one), which the API answers with 422.
        """


class CommentQueue(Protocol):
    """The comment queue between the webhook and the workers (Redis Streams, milestone 4a).

    Delivery is at least once. A comment handed to a consumer stays pending
    until it is acknowledged or dead-lettered, and ``reclaim`` hands a pending
    comment idle for too long to a consumer again, with its delivery count
    incremented. A comment may therefore be processed twice, which the inbox
    (``CommentInbox.claim``) makes harmless. The order is first come, first
    served at best: a reclaimed comment comes back after newer ones.
    """

    async def backlog(self) -> int:
        """The comments not acknowledged yet, delivered or not: what backpressure compares."""

    async def enqueue(self, comments: Sequence[QueuedComment]) -> None:
        """Append the comments in order.

        Raises ``ServiceUnavailable`` when the queue did not take them all. Some
        may be queued already: the API answers 503, and the platform's retry
        is deduplicated by the inbox.
        """

    async def receive(
        self, consumer: str, *, max_messages: int, block_seconds: float
    ) -> list[QueueDelivery]:
        """Hand ``consumer`` at most ``max_messages`` new comments, in order.

        Waits up to ``block_seconds`` for the first one, and returns an empty
        list if none came.
        """

    async def ack(self, delivery: QueueDelivery) -> None:
        """Settle a delivery for good; acknowledging it again does nothing."""

    async def reclaim(
        self, consumer: str, *, min_idle_seconds: float, max_messages: int
    ) -> list[QueueDelivery]:
        """Hand ``consumer`` the pending comments idle for ``min_idle_seconds`` or more.

        At most ``max_messages``, oldest first, each with its delivery count incremented.
        """

    async def dead_letter(self, delivery: QueueDelivery) -> None:
        """Move a delivery to the dead-letter queue, which settles it.

        The caller then marks its inbox row with ``CommentInbox.dead_letter``.
        """


# --- Services around a comment ---------------------------------------------------------


class IntentFallback(Protocol):
    """Settles an ambiguous comment with an LLM (Claude, milestone 4c), when one is configured.

    It never raises for a failure of the model or of its provider: every
    attempt returns a receipt with a closed ``LlmOutcome`` within the
    request's timeout, ``ok`` with a proposal, anything else without one. Only
    the comment's text and the catalog reach the model, never the buyer's
    identity. The proposal is not trusted: the domain checks it against the
    catalog before anything is reserved.
    """

    async def resolve(self, request: FallbackRequest) -> FallbackReceipt:
        """One attempt to read the comment; see the class docstring."""


class NullIntentFallback:
    """The fallback of a deployment without an LLM: every ambiguity stays unresolved.

    It lives here rather than among the test fakes because the worker runs it
    in production whenever no LLM is configured. Its ``disabled`` receipt turns
    the comment into an ``ambiguous_unresolved`` rejection.
    """

    async def resolve(self, request: FallbackRequest) -> FallbackReceipt:
        return FallbackReceipt(LlmOutcome.DISABLED)


class EventPublisher(Protocol):
    """Publishes session events to the stream the SSE endpoint relays (milestone 4b).

    Called after the commit that caused the event, never inside a unit. Best
    effort: a published event reaches every open stream of its session, in
    order, but a failure raises ``ServiceUnavailable`` and is not retried. The
    caller logs it, and the dashboard catches up from its next snapshot; its
    reducer upserts, so an event seen twice is harmless too.
    """

    async def publish(self, event: SessionEvent) -> None:
        """Append one event to its session's stream."""


class BuyerNotifier(Protocol):
    """Delivers an order's reference and access token to its buyer on the platform.

    Delivery is at least once. A failure raises ``ServiceUnavailable``: the
    comment is not acknowledged, and its redelivery notifies again with the
    same token, recomputed from the order id. A buyer may receive the same
    message twice, never two tokens for one order, and the token never
    reaches a log line.
    """

    async def notify(self, notification: BuyerNotification) -> None:
        """Send one notification."""


# --- Payment and shipping providers ----------------------------------------------------


class PaymentProvider(Protocol):
    """A hosted payment page: a mock by default, Stripe Checkout in test mode (milestone 5).

    ``create_checkout`` is idempotent on the request's idempotency key: one
    attempt always gets the same session, so a crash between creating a
    session and recording it is recovered by creating it again. Expiring a
    session settles the race between a late payment and the release of the
    stock (expire-first): stock is released only once the provider has
    expired the session. Transport failures raise ``ServiceUnavailable``, and
    an unknown session ``RecordNotFound``. PayPal and Viva are documented
    stubs that refuse to start.
    """

    @property
    def name(self) -> PaymentProviderName:
        """The provider, as recorded on each payment."""

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        """The open session of this attempt, created on the first call."""

    async def expire_checkout(self, provider_ref: str) -> bool:
        """Expire an open session.

        ``True`` when this call expired it; ``False`` when the provider refused
        because the session is no longer open (paid, or expired already): read
        it back with ``get_checkout`` to decide.
        """

    async def get_checkout(self, provider_ref: str) -> CheckoutSession:
        """The session as the provider sees it now."""

    async def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> PaymentEvent:
        """Check the signature before reading the body, then reduce the event to its allow-list.

        Raises ``InvalidSignature`` when the signature does not hold, and
        ``ValueError`` for a verified body that is no event.
        """


class CarrierProvider(Protocol):
    """A carrier for paid orders: a stateless mock (milestone 6); DPD, Chronopost and DHL are stubs.

    ``create_shipment`` is idempotent per order: creating again for the same
    order returns the same shipment, so a crash between creating a shipment
    and recording it is harmless. A shipment's status only moves along
    ``SHIPMENT_TRANSITIONS``. Transport failures raise ``ServiceUnavailable``,
    and an unknown shipment ``RecordNotFound``.
    """

    @property
    def name(self) -> CarrierName:
        """The carrier, as recorded on each shipment."""

    async def create_shipment(self, request: ShipmentRequest) -> ShipmentRef:
        """The shipment of this order, created on the first call."""

    async def get_tracking(self, ref: ShipmentRef) -> TrackingUpdate:
        """The shipment's current status at the carrier."""

    async def cancel_shipment(self, ref: ShipmentRef) -> None:
        """Cancel a shipment not yet in transit; ``InvalidTransition`` once it is."""


# --- Repositories ----------------------------------------------------------------------


class CommentInbox(Protocol):
    """The ``comments`` inbox: one row per platform comment, its deduplication and its lease.

    A comment is recorded once per (platform, external_comment_id), whatever
    the number of its deliveries, and settles once, from ``received`` only:
    processed (by ``OrderRepository.reserve``), rejected or dead-lettered. The
    lease gives one delivery at a time the right to work on a received
    comment, so that at most one LLM call happens per comment.
    """

    async def claim(
        self, comment: QueuedComment, *, consumer: str, now: datetime, lease: timedelta
    ) -> CommentClaim:
        """Unit 1: record the comment if it is new, then try to take its lease.

        ``consumer`` gets the lease, until ``now + lease``, when the comment is
        still received and no lease runs at ``now``; ``acquired`` says whether
        it did. A known comment keeps the text of its first delivery.
        """

    async def get(self, comment_id: UUID) -> InboxComment | None:
        """The row, or ``None``."""

    async def record_llm_receipt(
        self, comment_id: UUID, receipt: FallbackReceipt
    ) -> FallbackReceipt:
        """Unit 2: store the receipt unless the comment has one, and return the stored one.

        A second attempt never overwrites the first, and the caller decides on
        the receipt it gets back, not on the one it sent.
        """

    async def reject(
        self, comment_id: UUID, reason: RejectionReason, *, session_id: UUID | None
    ) -> TransitionOutcome:
        """Settle a received comment as rejected; a second rejection keeps the first reason."""

    async def dead_letter(self, platform: Platform, external_comment_id: str) -> TransitionOutcome:
        """Settle a received comment as dead-lettered, once its last delivery failed."""


class LiveSessionRepository(Protocol):
    """Live sessions, and the catalog of their products as the parser reads it."""

    async def get(self, session_id: UUID) -> LiveSessionRecord | None:
        """The row, or ``None``."""

    async def find_by_live_id(
        self, platform: Platform, external_live_id: str
    ) -> LiveSessionRecord | None:
        """The session a comment belongs to (``uq_live_sessions_platform_external_live_id``)."""

    async def load_catalog(self, session_id: UUID) -> Catalog:
        """The session's products grouped by code, each code's variants in creation order."""

    async def set_status(
        self, session_id: UUID, target: LiveSessionStatus, *, now: datetime
    ) -> TransitionOutcome:
        """Start or end a session (draft, live, ended), stamping ``started_at`` or ``ended_at``."""


class ProductStockRepository(Protocol):
    """The products of the sessions and their stock counters.

    Reservations, sales and releases change the counters only through the
    order methods of ``OrderRepository``, in the same unit as the order.
    """

    async def get(self, product_id: UUID) -> ProductRecord | None:
        """The row, or ``None``."""

    async def list_for_session(self, session_id: UUID) -> list[ProductRecord]:
        """The session's products, in creation order."""

    async def restock(self, product_id: UUID, delta: int) -> ProductRecord:
        """Add ``delta`` (negative to remove) to the stock total.

        Raises ``StockConflict`` when the total would fall below the reserved
        and sold units.
        """


class OrderReservations(Protocol):
    """Unit 3 of a comment, and the reads of what it recorded: what ``ReserveStock`` needs.

    A buyer has at most one pending order per product of a session
    (``uq_orders_pending_buyer_product``), and every order has a reference no
    other order has. ``OrderRepository`` adds what happens to an order after
    its reservation; the milestone of each step implements it.
    """

    async def reserve(self, request: ReservationRequest) -> ReservationResult | None:
        """Unit 3: settle the comment as processed, reserve, record the intent, open the order.

        The stock is reserved when the session is live and enough is
        available; the intent is then ``reserved`` and a pending order opens
        under a fresh reference, for the comment's author, until
        ``reserved_until``. The intent is ``out_of_stock`` when the stock is
        short or the session no longer live, and ``already_reserved`` when the
        buyer already has a pending order for the product; neither changes the
        stock, and short stock is checked first. Returns ``None``, having
        written nothing, when the comment is no longer received (another
        delivery settled it). Raises ``ServiceUnavailable``, having written
        nothing, when three reference draws all collide.
        """

    async def find_reservation(self, comment_id: UUID) -> ReservationResult | None:
        """What unit 3 recorded for a comment, with the product's current stock, or ``None``."""

    async def get(self, order_id: UUID) -> OrderRecord | None:
        """The row, or ``None``."""

    async def get_by_reference(self, reference: OrderReference) -> OrderRecord | None:
        """The order with this reference, or ``None``."""


class OrderRepository(OrderReservations, Protocol):
    """Purchase intents and orders, with the stock effects of their transitions."""

    async def mark_buyer_notified(self, order_id: UUID, *, now: datetime) -> OrderRecord:
        """Stamp ``buyer_notified_at`` once; a later call keeps the first time."""

    async def set_shipping_address(self, order_id: UUID, address: ShippingAddress) -> OrderRecord:
        """Record where the buyer wants the order delivered."""

    async def expire_due(self, *, now: datetime, limit: int) -> list[OrderRecord]:
        """First step of the expiry sweep, and the whole of it until milestone 5.

        Expires at most ``limit`` pending orders whose reservation ended
        before ``now`` and that have no pending payment, lowest id first,
        releasing their reserved stock. Returns the expired orders.
        """

    async def record_payment(self, order_id: UUID, *, now: datetime) -> OrderTransition:
        """A confirmed payment, stamped ``paid_at``.

        A pending order becomes paid, its reserved units sold; an expired order
        becomes ``paid_after_expiry``, with no stock effect (its refund is
        manual, by runbook). A replayed payment is a ``NOOP``.
        """


class PaymentRepository(Protocol):
    """Payment attempts: one row per attempt, at most one pending per order."""

    async def open_attempt(
        self,
        order_id: UUID,
        *,
        provider: PaymentProviderName,
        return_url: str,
        now: datetime,
        grace: timedelta,
        max_attempts: int,
    ) -> AttemptOpening:
        """StartCheckout, step 1.

        An attempt already pending is returned as it is: a second click reuses
        its redirect URL. Otherwise, if the order waits for a payment and its
        reservation has not ended at ``now``, the reservation is extended to
        at least ``now + grace`` and attempt n + 1 opens, for the order's
        total, unless ``max_attempts`` attempts were made already.
        """

    async def record_checkout(
        self, payment_id: UUID, *, provider_ref: str, redirect_url: str
    ) -> PaymentRecord:
        """Store the provider's session of an attempt once; a repeat keeps the first."""

    async def get_pending(self, order_id: UUID) -> PaymentRecord | None:
        """The order's pending attempt, or ``None``."""

    async def find_by_provider_ref(self, provider_ref: str) -> PaymentRecord | None:
        """The attempt of a provider's session, or ``None``."""

    async def set_status(
        self, payment_id: UUID, target: PaymentStatus, *, provider_payment_ref: str | None = None
    ) -> TransitionOutcome:
        """Settle a pending attempt as succeeded, failed or expired."""


class ShipmentRepository(Protocol):
    """Shipments: at most one per order (``uq_shipments_order_id``)."""

    async def add(self, order_id: UUID, ref: ShipmentRef) -> ShipmentRecord:
        """Record the carrier's shipment of an order; a second call returns the first record."""

    async def get_by_order(self, order_id: UUID) -> ShipmentRecord | None:
        """The order's shipment, or ``None``."""

    async def list_in_flight(self, *, limit: int) -> list[ShipmentRecord]:
        """Shipments neither delivered nor cancelled, lowest id first: what tracking polls."""

    async def set_status(
        self, shipment_id: UUID, target: ShipmentStatus, *, event_at: datetime
    ) -> TransitionOutcome:
        """Move a shipment one step along its machine, stamping ``last_event_at``."""


class WebhookEventRegistry(Protocol):
    """The durable registry of payment webhooks, ``processed_webhook_events``: the worker's outbox.

    An event is recorded once per (provider, event_id) and processed once.
    Two readers may see the same pending event: applying it is idempotent
    (``OrderRepository.record_payment`` is a compare-and-set) and
    ``mark_processed`` settles it once.
    """

    async def record(self, event: PaymentEvent, *, now: datetime) -> bool:
        """Record a verified event: ``True`` the first time, ``False`` for a counted redelivery."""

    async def pending(self, *, limit: int) -> list[RegisteredEvent]:
        """At most ``limit`` unprocessed events, oldest first."""

    async def mark_processed(
        self,
        provider: PaymentProviderName,
        event_id: str,
        outcome: WebhookOutcome,
        *,
        now: datetime,
    ) -> bool:
        """``True`` when this call processed the event, ``False`` when it was processed already."""

    async def record_failure(
        self, provider: PaymentProviderName, event_id: str, *, max_attempts: int, now: datetime
    ) -> RegisteredEvent:
        """Count a failed attempt; at ``max_attempts``, the event is processed as ``failed``."""
