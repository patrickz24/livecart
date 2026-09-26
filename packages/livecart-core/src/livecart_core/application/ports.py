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
from decimal import Decimal
from typing import Final, Literal, Protocol
from uuid import UUID

from livecart_core.application.dto import (
    AttemptOpening,
    BuyerNotification,
    BuyerOrderRecord,
    CarrierName,
    CheckoutRequest,
    CheckoutSession,
    CommentClaim,
    ExpiredReservations,
    FallbackReceipt,
    FallbackRequest,
    IdempotencyKey,
    IdempotencyRecord,
    InboxComment,
    IncomingComment,
    LiveSessionChanges,
    LiveSessionRecord,
    LiveSessionTransition,
    LlmBudgetUsage,
    NewLiveSession,
    NewProduct,
    OrderRecord,
    OrderTransition,
    Page,
    ParseSummary,
    PaymentEvent,
    PaymentProviderName,
    PaymentRecord,
    Platform,
    ProductChanges,
    ProductRecord,
    QueuedComment,
    QueueDelivery,
    RateLimitKey,
    RefreshTokenRecord,
    RegisteredEvent,
    ReservationRequest,
    ReservationResult,
    SellerRecord,
    SessionActivity,
    SessionEvent,
    ShipmentRecord,
    ShipmentRef,
    ShipmentRequest,
    ShippingAddress,
    StreamedEvent,
    TrackingUpdate,
    WebhookOutcome,
    WindowCount,
)
from livecart_core.domain.catalog import Catalog
from livecart_core.domain.live_sessions import CatalogConflict
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
    """A restock that would bring a product's stock total below its reserved and sold units.

    ``above_maximum`` marks the other bound: a total above ``MAX_STOCK_TOTAL``.
    """

    def __init__(self, message: str, *, above_maximum: bool = False) -> None:
        super().__init__(message)
        self.above_maximum = above_maximum


class SellerConflict(PortError):
    """A new seller whose e-mail (whatever its case) or slug another seller has already."""

    def __init__(self, field: Literal["email", "slug"]) -> None:
        super().__init__(f"another seller has this {field}")
        self.field: Literal["email", "slug"] = field


class LiveSessionConflict(PortError):
    """A session whose platform and live id another session has already, whatever its seller.

    ``uq_live_sessions_platform_external_live_id``: the pair is how a comment
    finds its session, so it names one session on the whole platform.
    """


class OrderClosed(PortError):
    """A change an order takes only while it waits for its payment, asked of one paid or expired."""


class ProductConflict(PortError):
    """A product the session's catalog cannot hold; ``conflict`` says why."""

    def __init__(self, conflict: CatalogConflict) -> None:
        super().__init__(f"the catalog cannot hold this product: {conflict}")
        self.conflict = conflict


# --- Time ------------------------------------------------------------------------------


class Clock(Protocol):
    """The current time: the system clock in production, a settable one in tests."""

    def now(self) -> datetime:
        """A timezone-aware UTC datetime: every deadline and lease end is computed from it."""


# --- Seller authentication -------------------------------------------------------------


class PasswordHasher(Protocol):
    """Hashes and checks seller passwords: argon2id, off the event loop, with bounded concurrency.

    ``verify`` costs the same whatever its answer, and the same again for an
    e-mail that names no seller: a login cannot tell a wrong password from an
    unknown e-mail by its timing.
    """

    async def hash(self, password: str) -> str:
        """A new salted hash of ``password``, to store."""

    async def verify(self, password_hash: str | None, password: str) -> bool:
        """Whether ``password`` matches ``password_hash``.

        ``None`` stands for an account that does not exist: the password is
        checked against a dummy hash of the same cost, and the answer is
        ``False``. A stored value that is no hash at all (a locked account)
        answers ``False`` after the same work.
        """


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
    served at best: a reclaimed comment comes back after newer ones. Nothing
    caps the queue's length: the API stops enqueueing once the backlog
    reaches its limit (backpressure), and ``trim`` drops the settled history.
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

        A delivery settled already (acknowledged or dead-lettered) is left as
        it is. The caller then marks its inbox row with ``CommentInbox.dead_letter``.
        """

    async def trim(self) -> int:
        """Drop the settled history: entries delivered and then acknowledged or dead-lettered.

        Never an entry that is new or pending. Returns how many entries went;
        an adapter may keep some settled ones for a later trim.
        """


# --- Services around a comment ---------------------------------------------------------


class IntentFallback(Protocol):
    """Settles an ambiguous comment with an LLM (Claude, milestone 4c), when one is configured.

    It never raises for a failure of the model or of its provider: every
    attempt returns a receipt with a closed ``LlmOutcome`` within the
    request's timeout, ``ok`` with a proposal, anything else without one.
    Only the budget's own store may fail a call (``LlmGuard``), with
    ``ServiceUnavailable``, as any unit's would. Only
    the comment's text and the catalog reach the model, never the buyer's
    identity. The proposal is not trusted: the domain checks it against the
    catalog before anything is reserved.
    """

    async def resolve(self, request: FallbackRequest) -> FallbackReceipt:
        """One attempt to read the comment; see the class docstring."""


class LlmBudget(Protocol):
    """The LLM's spend, shared by every worker (Redis): calls per session, dollars per UTC day.

    The budget only counts: what a count allows is ``LlmGuard``'s rule. The
    dollars are kept in whole micro-dollars, each cost rounded up
    (``to_micro_usd``). A session's count lasts two days from its first call,
    longer than any live, and so does a day's spend; the day is the UTC date
    of ``now``. A failure of the store raises ``ServiceUnavailable``.
    """

    async def usage(self, session_id: UUID, *, now: datetime) -> LlmBudgetUsage:
        """The session's calls so far and the day's spend, counting nothing."""

    async def count_call(self, session_id: UUID, *, now: datetime) -> LlmBudgetUsage:
        """Count one call of the session; the usage returned includes it."""

    async def add_spend(self, cost_usd: Decimal, *, now: datetime) -> Decimal:
        """Add a call's cost to the spend of ``now``'s day; return the day's spend."""


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
    order, but a failure raises ``ServiceUnavailable`` and is not retried: the
    commit stands, the failure is logged and counted, and the dashboard
    catches up from its next snapshot. Its reducer upserts, so an event seen
    twice is harmless too.
    """

    async def publish(self, event: SessionEvent) -> None:
        """Append one event to its session's stream."""


# The cursor of a session whose stream holds no event: every event comes after it.
START_CURSOR: Final = "0-0"


class SessionEventStream(Protocol):
    """Reads the stream of a live session's events, as the SSE endpoint relays them.

    Each event gets an id when it is appended (``StreamedEvent.event_id``,
    ``<milliseconds>-<sequence>``), increasing within its session: the SSE
    id, which a client sends back as ``Last-Event-ID`` to resume. A stream
    keeps about the last 15 minutes of events, and goes altogether a day
    after its last event (an hour once its session has ended), so a reader
    that resumes checks ``has_gap`` first: past a gap, it takes a new
    snapshot rather than miss events silently. A malformed cursor raises
    ``ValueError``, a failure of the stream ``ServiceUnavailable``.
    """

    async def cursor(self, session_id: UUID) -> str:
        """The id of the session's last event, or ``START_CURSOR`` when it has none.

        Read before the snapshot it goes with: every event after it is then
        either in the snapshot or read after it, some maybe both.
        """

    async def read(
        self, session_id: UUID, *, after: str, max_events: int, block_seconds: float
    ) -> list[StreamedEvent]:
        """At most ``max_events`` events after ``after``, oldest first.

        When none is there yet, waits up to ``block_seconds`` for the first
        one, and returns an empty list once the wait is over.
        """

    async def has_gap(self, session_id: UUID, *, after: str) -> bool:
        """Whether events after ``after`` may be gone: trimmed, or the whole stream expired.

        On the safe side: a cursor older than every event the stream still
        holds is a gap once the stream has trimmed any, even if the cursor
        was the last of them; so is a cursor past the stream's last event,
        or from a stream since expired and started again. A false gap costs
        one snapshot, a missed one would lose events.
        """


class SessionEventLog(EventPublisher, SessionEventStream, Protocol):
    """Both sides of the session streams, as one adapter provides them to the API."""


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
    """A hosted payment page: the mock by default, Stripe Checkout in test mode.

    Four calls, whatever the provider, over provider-neutral records
    (``CheckoutRequest``, ``CheckoutSession``, ``PaymentEvent``): create a
    hosted session, expire it, read it back, verify a webhook. No card data
    ever reaches LiveCart: the buyer pays on the provider's page.

    - ``create_checkout`` is idempotent on the request's idempotency key
      (``checkout-session:{order_id}:{attempt_no}``): one attempt always gets
      the same session, so a crash between creating a session and recording
      it is recovered by creating it again. A key sent again with other
      parameters raises ``ValueError``, as Stripe refuses it;
    - expiring a session settles the race between a late payment and the
      release of the stock (expire-first): stock is released only once the
      provider has expired the session, and a session the buyer paid first
      refuses to expire;
    - transport failures raise ``ServiceUnavailable``, and an unknown session
      ``RecordNotFound``.

    Every process builds the same adapter from ``PAYMENT_PROVIDER``, so the
    API and the worker see the same sessions: the provider keeps them, never
    the process. PayPal and Viva are documented stubs that refuse to start.
    """

    @property
    def name(self) -> PaymentProviderName:
        """The provider, as recorded on each payment."""

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        """The session of this attempt, created open on the first call, then as it stands."""

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
        ``ValueError`` for a verified body that is no event of this provider.
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


class SellerRepository(Protocol):
    """Seller accounts, unique by e-mail whatever its case, and by slug."""

    async def add(
        self, *, email: str, slug: str, display_name: str, password_hash: str, now: datetime
    ) -> SellerRecord:
        """Create an account from values the domain checked (``livecart_core.domain.sellers``).

        Raises ``SellerConflict`` when another seller has the e-mail, in any
        case, or the slug.
        """

    async def get(self, seller_id: UUID) -> SellerRecord | None:
        """The row, or ``None``."""

    async def find_by_email(self, email: str) -> SellerRecord | None:
        """The seller whose e-mail equals ``email`` ignoring case, or ``None``."""

    async def find_by_slug(self, slug: str) -> SellerRecord | None:
        """The seller whose storefront has this slug (``uq_sellers_slug``), or ``None``."""


class RefreshTokenRepository(Protocol):
    """The sellers' refresh tokens, stored as SHA-256 hashes, in one family per login.

    A token is used once: rotating it marks it used and stores its successor,
    in its family and with its expiry, as one unit that exactly one of several
    concurrent rotations of the token wins. Revoking a family revokes every
    token of it, the successor of a rotation committing meanwhile included.
    """

    async def issue(
        self, seller_id: UUID, token_hash: bytes, *, now: datetime, expires_at: datetime
    ) -> RefreshTokenRecord:
        """Store the first token of a new family: a login.

        Raises ``RecordNotFound`` when no seller has ``seller_id``.
        """

    async def rotate(
        self, token_hash: bytes, successor_hash: bytes, *, now: datetime
    ) -> RefreshTokenRecord | None:
        """Use the token if it is active at ``now``, store its successor, and return the successor.

        ``None``, having written nothing, when no token has the hash or when
        it is used, revoked or expired.
        """

    async def find(self, token_hash: bytes) -> RefreshTokenRecord | None:
        """The token with this hash, whatever its state, or ``None``."""

    async def get(self, token_id: UUID) -> RefreshTokenRecord | None:
        """The row, or ``None``."""

    async def revoke_family(self, family_id: UUID, *, now: datetime) -> int:
        """Revoke every token of the family not revoked yet; return how many this call revoked."""


class CommentInbox(Protocol):
    """The ``comments`` inbox: one row per platform comment, its deduplication and its lease.

    A comment is recorded once per (platform, external_comment_id), whatever
    the number of its deliveries, and settles once, from ``received`` only:
    processed (by ``OrderRepository.reserve``), rejected or dead-lettered. The
    lease gives one delivery at a time the right to work on a received
    comment, so that at most one LLM call happens per comment.
    """

    async def claim(
        self, comment: QueuedComment, *, holder: str, now: datetime, lease: timedelta
    ) -> CommentClaim:
        """Unit 1: record the comment if it is new, then try to take its lease.

        ``holder`` names one delivery (``ProcessComment`` passes its consumer
        and its delivery id). It gets the lease, until ``now + lease``, when
        the comment is still received and no other holder's lease runs at
        ``now``: a holder claiming again, such as a retry of its delivery,
        takes the lease again, its lease extended. ``acquired`` says whether it
        did. A known comment keeps the text of its first delivery.
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
        self,
        comment_id: UUID,
        reason: RejectionReason,
        *,
        session_id: UUID | None,
        parse: ParseSummary | None = None,
    ) -> TransitionOutcome:
        """Settle a received comment as rejected; a second rejection keeps the first reason.

        ``parse``, when the parser read the comment, is recorded with it.
        """

    async def dead_letter(self, comment: QueuedComment, *, now: datetime) -> TransitionOutcome:
        """Settle a comment as dead-lettered, once its last delivery failed.

        A received comment becomes dead-lettered; a comment no delivery ever
        recorded (each failed before unit 1 committed) is recorded
        dead-lettered at ``now``, so that the inbox keeps the durable trace of
        every dead letter, and a later delivery of it is a duplicate.
        """


class LiveSessionRepository(Protocol):
    """Live sessions, and the catalog of their products as the parser reads it.

    The methods that take a ``seller_id`` serve that seller's own requests:
    they filter every query by it, so another seller's session is
    ``RecordNotFound`` or ``None``, exactly as a session that does not exist.
    What a session in each status still takes is ``domain.live_sessions``'
    rule, checked in the unit that writes, with the session's row locked.
    """

    async def add(
        self, seller_id: UUID, session: NewLiveSession, *, now: datetime
    ) -> LiveSessionRecord:
        """Create a draft session.

        Raises ``LiveSessionConflict`` when another session has the platform
        and the live id, and ``RecordNotFound`` when no seller has ``seller_id``.
        """

    async def get(self, session_id: UUID) -> LiveSessionRecord | None:
        """The row, or ``None``."""

    async def get_for_seller(self, seller_id: UUID, session_id: UUID) -> LiveSessionRecord | None:
        """The seller's session, or ``None``."""

    async def list_for_seller(
        self, seller_id: UUID, *, limit: int, cursor: UUID | None = None
    ) -> Page[LiveSessionRecord]:
        """The seller's sessions, newest first, from ``cursor``: a previous ``next_cursor``."""

    async def update(
        self, seller_id: UUID, session_id: UUID, changes: LiveSessionChanges
    ) -> LiveSessionRecord:
        """Apply the changes to the seller's session.

        Raises ``RecordNotFound``, ``EditRefused`` when the session's status
        no longer takes the change, and ``LiveSessionConflict``.
        """

    async def set_status(
        self, seller_id: UUID, session_id: UUID, target: LiveSessionStatus, *, now: datetime
    ) -> LiveSessionTransition:
        """Start or end the seller's session (draft, live, ended), stamping its time.

        A compare-and-set: ``APPLIED``, ``NOOP`` when the session is there
        already, ``InvalidTransition`` otherwise, ``RecordNotFound``.
        """

    async def find_by_live_id(
        self, platform: Platform, external_live_id: str
    ) -> LiveSessionRecord | None:
        """The session a comment belongs to (``uq_live_sessions_platform_external_live_id``)."""

    async def load_catalog(self, session_id: UUID) -> Catalog:
        """The session's products grouped by code, each code's variants in creation order.

        Raises ``RecordNotFound`` when no session has ``session_id``.
        """

    async def find_current(self, seller_id: UUID) -> LiveSessionRecord | None:
        """The seller's live session that started last, what the storefront shows, or ``None``.

        A seller may run several at once, one per platform: the latest wins.
        """


class ProductStockRepository(Protocol):
    """The products of the sessions and their stock counters.

    Reservations, sales and releases change the counters only through the
    order methods of ``OrderRepository``, in the same unit as the order. The
    methods that take a ``seller_id`` filter by it, as those of
    ``LiveSessionRepository`` do, and write under the session's row lock:
    two writes to one catalog never interleave, while reservations, which
    never lock the session, go on.
    """

    async def add(
        self, seller_id: UUID, session_id: UUID, product: NewProduct, *, now: datetime
    ) -> ProductRecord:
        """Add a product to the seller's session.

        Raises ``RecordNotFound``, ``EditRefused`` for an ended session, and
        ``ProductConflict`` when the catalog cannot hold it.
        """

    async def get(self, product_id: UUID) -> ProductRecord | None:
        """The row, or ``None``."""

    async def get_for_seller(
        self, seller_id: UUID, session_id: UUID, product_id: UUID
    ) -> ProductRecord | None:
        """The product of the seller's session, or ``None``."""

    async def list_for_session(self, session_id: UUID) -> list[ProductRecord]:
        """The session's products, in creation order."""

    async def list_for_seller(
        self, seller_id: UUID, session_id: UUID, *, limit: int, cursor: UUID | None = None
    ) -> Page[ProductRecord]:
        """The products of the seller's session, in creation order.

        Raises ``RecordNotFound`` when the session is not the seller's.
        """

    async def update(
        self, seller_id: UUID, session_id: UUID, product_id: UUID, changes: ProductChanges
    ) -> ProductRecord:
        """Apply the changes to a product of the seller's session.

        Raises ``RecordNotFound``, ``EditRefused`` and ``ProductConflict``.
        """

    async def restock(
        self, seller_id: UUID, session_id: UUID, product_id: UUID, delta: int
    ) -> ProductRecord:
        """Add ``delta`` (negative to remove) to the stock of a product of the seller's session.

        One conditional update decides, so a restock never races a
        reservation. Raises ``RecordNotFound``, ``EditRefused`` for an ended
        session, and ``StockConflict`` when the total would fall below the
        reserved and sold units or rise above ``MAX_STOCK_TOTAL``.
        """


# How long the first answer to an idempotency key is kept, and replayed: a day.
IDEMPOTENCY_TTL_SECONDS: Final = 86_400


class IdempotencyStore(Protocol):
    """The first answer to each idempotency key of a seller, kept for a day (Redis, ``SET NX EX``).

    ``claim`` is the one atomic step: of several requests under one key,
    exactly one gets ``None`` and runs; the others read what it left, its
    fingerprint and, once it answered, its answer. A failure of the store
    raises ``ServiceUnavailable``.
    """

    async def claim(self, key: IdempotencyKey, fingerprint: str) -> IdempotencyRecord | None:
        """Claim ``key`` for the request of ``fingerprint``.

        ``None`` when this call claimed it; otherwise the record of the
        request that did, whose ``response`` is ``None`` while it runs.
        """

    async def save(self, key: IdempotencyKey, record: IdempotencyRecord) -> None:
        """Replace the claim with the claiming request's answer, keeping the claim's expiry."""

    async def release(self, key: IdempotencyKey) -> None:
        """Drop the claim of a request that failed without answering, so the key may run again."""


class RateLimiter(Protocol):
    """Fixed-window counters, one per key, shared by every process (Redis, one Lua script).

    A window starts with the first hit of its key and ends ``window_seconds``
    later, whatever the hits in between; the next hit then starts a new one.
    The limiter only counts: what a count allows is the caller's rule
    (``RateLimitRule``). A failure of the store raises ``ServiceUnavailable``,
    and the caller decides whether the request goes on.
    """

    async def hit(self, key: RateLimitKey, *, window_seconds: int) -> WindowCount:
        """Count one hit of ``key``; the count returned includes it."""

    async def peek(self, key: RateLimitKey) -> WindowCount:
        """The count of ``key``'s running window, without counting a hit."""


# How many order references unit 3 draws before it gives up with ServiceUnavailable.
# With a million orders among the 2**30 references, three draws all collide
# about once in a billion reservations, so running out means a bug, or a
# nearly full space: the comment is redelivered rather than lost.
REFERENCE_DRAWS: Final = 3


class OrderReservations(Protocol):
    """Unit 3 of a comment, the reads of what it recorded, and the buyer's notice.

    What ``ReserveStock`` and ``ProcessComment`` need.

    A buyer has at most one pending order per product of a session
    (``uq_orders_pending_buyer_product``), and every order has a reference no
    other order has. ``OrderRepository`` adds what happens to an order after
    its reservation; the milestone of each step implements it.
    """

    async def reserve(self, request: ReservationRequest) -> ReservationResult | None:
        """Unit 3: settle the comment as processed, reserve, record the intent, open the order.

        The comment is settled with its session and, when the request carries
        one, what the parser said of it. The stock is reserved when the
        session is live and enough is available; the intent is then
        ``reserved`` and a pending order opens under a fresh reference, for
        the comment's author, until ``reserved_until``. The intent is
        ``out_of_stock`` when the stock is short or the session no longer
        live, and ``already_reserved`` when the buyer already has a pending
        order for the product; neither changes the stock, and short stock is
        checked first. Returns ``None``, having
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

    async def mark_buyer_notified(self, order_id: UUID, *, now: datetime) -> OrderRecord:
        """Stamp ``buyer_notified_at`` once; a later call keeps the first time.

        Raises ``RecordNotFound`` when no order has ``order_id``.
        """


class OrderLookup(Protocol):
    """An order as its buyer reads it, found by the reference read aloud on the live.

    The record carries the hash of the order's access token: the reference
    alone shows the order's status, and whatever needs more is checked
    against that hash first (``livecart_core.order_tokens``).
    """

    async def find_by_reference(self, reference: OrderReference) -> BuyerOrderRecord | None:
        """The order with this reference and the product it holds, or ``None``."""


# The most intents, rejections and orders a snapshot holds, of each: the newest.
SNAPSHOT_LIMIT: Final = 200


class LiveSessionActivity(Protocol):
    """The snapshot of a live session, as the SSE endpoint sends it before the session's events."""

    async def snapshot(self, session_id: UUID, *, limit: int) -> SessionActivity:
        """The session, its products and its latest ``limit`` intents, rejections and orders.

        One consistent read (a single snapshot of the database), so that an
        order and its product's stock agree. Raises ``RecordNotFound`` when no
        session has ``session_id``.
        """


# The most orders one batch of the expiry sweep expires, in one transaction.
EXPIRY_BATCH: Final = 100


class ReservationExpiry(Protocol):
    """The release of the reservations nobody paid: what ``ExpireReservations`` needs.

    Several sweeps, one per worker, run at once: each order is expired, and
    its stock released, exactly once, by whichever takes it first.
    """

    async def expire_due(self, *, now: datetime, limit: int) -> ExpiredReservations:
        """First step of the expiry sweep, and the whole of it until milestone 5.

        One unit: expires at most ``limit`` pending orders whose reservation
        ended before ``now`` and that have no pending payment, lowest id
        first, skipping those another sweep holds, and releases their
        reserved stock, product by product. Returns the expired orders and
        the counters of each product after the release.
        """


class BuyerOrders(OrderLookup, Protocol):
    """What the buyer's routes read and change of an order: the lookup, and the address."""

    async def set_shipping_address(self, order_id: UUID, address: ShippingAddress) -> OrderRecord:
        """Record where the buyer wants the order delivered, replacing any earlier address.

        Only while the order waits for its payment: once it is paid (the
        shipment is created from the address then) or expired, raises
        ``OrderClosed``. Raises ``RecordNotFound`` when no order has ``order_id``.
        """


class OrderRepository(OrderReservations, BuyerOrders, ReservationExpiry, Protocol):
    """Purchase intents and orders, with the stock effects of their transitions."""

    async def record_payment(self, order_id: UUID, *, now: datetime) -> OrderTransition:
        """A confirmed payment, stamped ``paid_at``.

        A pending order becomes paid, its reserved units sold; an expired order
        becomes ``paid_after_expiry``, with no stock effect (its refund is
        manual, by runbook). A replayed payment is a ``NOOP``.
        """


class PaymentAttempts(Protocol):
    """Payment attempts as a checkout opens them: what ``StartCheckout`` and the bridge page need.

    One row per attempt, numbered from 1, at most one pending per order
    (``uq_payments_order_id_pending``). The attempts of one order open one at
    a time: each opening holds the order's row until it commits.
    """

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
        """StartCheckout, step 1: one unit, before any call to the provider.

        An attempt already pending is returned as it is, the reservation
        untouched: a second click reuses its redirect URL. Otherwise, if the
        order waits for a payment and its reservation has not ended at
        ``now``, the reservation is extended to at least ``now + grace``
        (``GREATEST``, never shortened) and attempt n + 1 opens, pending, for
        the order's total, unless ``max_attempts`` attempts were made already.
        Raises ``RecordNotFound`` when no order has ``order_id``.
        """

    async def record_checkout(
        self, payment_id: UUID, *, provider_ref: str, redirect_url: str
    ) -> PaymentRecord:
        """Store the provider's session of an attempt once; a repeat keeps the first.

        StartCheckout, step 2, once the provider created the session. Raises
        ``RecordNotFound`` when no attempt has ``payment_id``.
        """

    async def get_pending(self, order_id: UUID) -> PaymentRecord | None:
        """The order's pending attempt, or ``None``."""

    async def latest(self, order_id: UUID) -> PaymentRecord | None:
        """The order's last attempt, whatever its status, or ``None`` before the first."""


class PaymentRepository(PaymentAttempts, Protocol):
    """Payment attempts, and how the provider's events settle them."""

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


class WebhookEventRecorder(Protocol):
    """What the payment webhook writes to the registry: each verified event, once.

    One unit per call, on PostgreSQL alone, so that a provider's webhook is
    acknowledged whether Redis is up or not.
    """

    async def record(self, event: PaymentEvent, *, now: datetime) -> bool:
        """Record a verified event: ``True`` the first time, ``False`` for a counted redelivery.

        One row per (provider, event_id), however many deliveries; a
        redelivery only counts itself (``delivery_count``), and keeps the
        payload, the type and the processing of the first.
        """


class WebhookEventRegistry(WebhookEventRecorder, Protocol):
    """The durable registry of payment webhooks, ``processed_webhook_events``: the worker's outbox.

    An event is recorded once per (provider, event_id) and processed once.
    Two readers may see the same pending event: applying it is idempotent
    (``OrderRepository.record_payment`` is a compare-and-set) and
    ``mark_processed`` settles it once.
    """

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
