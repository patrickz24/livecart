"""In-memory fakes of every application port: the test doubles of every milestone.

Each fake does what its port's docstring promises and no more, so that a use
case tested against it also holds against the real adapter. They are
deterministic: identifiers are sequential UUIDs, time comes from a
``FakeClock``, order references from an injectable random source. The
repositories share one ``InMemoryDatabase``, as the SQL adapters share one
PostgreSQL, and none of their methods awaits, so each one is atomic under
asyncio, like the transaction it stands for. Rows are frozen records, replaced
whole on every change: a record a fake returned never changes behind a test.

Failures are scripted: ``fail_next(n)`` makes the next n calls of a service
fake raise ``ServiceUnavailable``, with no side effect.
"""

import asyncio
import hashlib
import hmac
import secrets
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from livecart_core.application.dto import (
    AttemptOpening,
    AttemptOpeningKind,
    BuyerNotification,
    CarrierName,
    CheckoutRequest,
    CheckoutSession,
    CheckoutStatus,
    CommentBatch,
    CommentClaim,
    FallbackReceipt,
    FallbackRequest,
    InboxComment,
    IncomingComment,
    LiveSessionRecord,
    LlmProposal,
    OrderRecord,
    OrderTransition,
    PaymentEvent,
    PaymentProviderName,
    PaymentRecord,
    Platform,
    ProductRecord,
    PurchaseIntentRecord,
    QueuedComment,
    QueueDelivery,
    RegisteredEvent,
    ReservationRequest,
    ReservationResult,
    SessionEvent,
    SessionEventType,
    ShipmentRecord,
    ShipmentRef,
    ShipmentRequest,
    ShippingAddress,
    StockLevel,
    TrackingUpdate,
    WebhookOutcome,
)
from livecart_core.application.ports import (
    REFERENCE_DRAWS,
    Clock,
    InvalidSignature,
    RecordNotFound,
    ServiceUnavailable,
    StockConflict,
)
from livecart_core.domain.catalog import Catalog, CatalogProduct
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.money import Money
from livecart_core.domain.outcomes import IntentOutcome, LlmOutcome, RejectionReason
from livecart_core.domain.reference import (
    OrderReference,
    RandomSource,
    generate_order_reference,
)
from livecart_core.domain.state_machines import (
    COMMENT_TRANSITIONS,
    LIVE_SESSION_TRANSITIONS,
    ORDER_TRANSITIONS,
    PAYMENT_TRANSITIONS,
    SHIPMENT_TRANSITIONS,
    CommentStatus,
    LiveSessionStatus,
    OrderStatus,
    PaymentStatus,
    ShipmentStatus,
    TransitionOutcome,
    transition,
)

# The day milestone 2 landed, at noon UTC: any fixed instant would do.
FAKE_CLOCK_START: Final = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
# The header the fake adapters read their signature from.
FAKE_SIGNATURE_HEADER: Final = "x-fake-signature"


def _require_aware(moment: datetime) -> datetime:
    if moment.utcoffset() is None:
        raise ValueError(f"{moment!r} is naive: every time in LiveCart is timezone-aware")
    return moment


def _require_positive(name: str, value: float) -> None:
    if not value > 0:
        raise ValueError(f"{name} must be positive, not {value}")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """A header by name, whatever its case, as HTTP headers are."""
    return next((value for key, value in headers.items() if key.lower() == name), None)


def _sign(key: bytes, body: bytes) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def _check_signature(headers: Mapping[str, str], body: bytes, key: bytes) -> None:
    signature = _header(headers, FAKE_SIGNATURE_HEADER)
    if signature is None or not hmac.compare_digest(signature, _sign(key, body)):
        raise InvalidSignature("the fake signature does not match the body")


class _ScriptedFailures:
    """``fail_next(n)``: the next n calls that check it raise ``ServiceUnavailable``."""

    _failures_left: int = 0

    def fail_next(self, count: int = 1) -> None:
        _require_positive("count", count)
        self._failures_left += count

    def _maybe_fail(self, operation: str) -> None:
        if self._failures_left:
            self._failures_left -= 1
            raise ServiceUnavailable(f"{type(self).__name__}.{operation}: scripted failure")


# --- Clock -----------------------------------------------------------------------------


class FakeClock:
    """A clock that only moves when the test says so."""

    def __init__(self, now: datetime = FAKE_CLOCK_START) -> None:
        self._now = _require_aware(now)

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = _require_aware(now)

    def advance(self, delta: timedelta | float) -> datetime:
        """Move forward by a ``timedelta`` or a number of seconds, and return the new time."""
        step = delta if isinstance(delta, timedelta) else timedelta(seconds=delta)
        if step < timedelta(0):
            raise ValueError("advance() only moves forward; set() goes anywhere")
        self._now += step
        return self._now


# --- Comment ingestion -----------------------------------------------------------------


class FakePlatformAdapter:
    """A platform whose webhook body is the common format, signed by an HMAC of the body.

    ``signed(batch)`` builds headers and a body this adapter accepts.
    """

    def __init__(
        self, platform: Platform = Platform.SIMULATOR, *, key: bytes = b"fake-platform-key"
    ) -> None:
        self._platform = platform
        self._key = key

    @property
    def platform(self) -> Platform:
        return self._platform

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        _check_signature(headers, raw_body, self._key)

    def parse(self, raw_body: bytes) -> list[IncomingComment]:
        return list(CommentBatch.model_validate_json(raw_body).comments)

    def signed(self, batch: CommentBatch) -> tuple[dict[str, str], bytes]:
        body = batch.model_dump_json().encode()
        return {FAKE_SIGNATURE_HEADER: _sign(self._key, body)}, body


@dataclass(slots=True)
class _QueueEntry:
    sequence: int
    comment: QueuedComment
    delivery_count: int = 0
    delivered_at: datetime | None = None

    @property
    def delivery_id(self) -> str:
        # The shape of a Redis stream entry id.
        return f"{self.sequence}-0"


class FakeCommentQueue(_ScriptedFailures):
    """An at-least-once queue with a pending list, idle-time reclaim and a dead-letter list.

    It never blocks: ``receive`` on an empty queue returns an empty list at
    once. Idle time is measured on the clock it is given.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._sequence = 0
        self._new: deque[_QueueEntry] = deque()
        self._pending: dict[str, _QueueEntry] = {}
        self.acknowledged: list[str] = []
        self.dead_letters: list[QueueDelivery] = []

    @property
    def pending(self) -> list[QueueDelivery]:
        """The deliveries neither acknowledged nor dead-lettered, oldest first."""
        return [self._delivery(entry) for entry in self._pending_in_order()]

    async def backlog(self) -> int:
        return len(self._new) + len(self._pending)

    async def enqueue(self, comments: Sequence[QueuedComment]) -> None:
        self._maybe_fail("enqueue")
        for comment in comments:
            self._sequence += 1
            self._new.append(_QueueEntry(self._sequence, comment))

    async def receive(
        self, consumer: str, *, max_messages: int, block_seconds: float
    ) -> list[QueueDelivery]:
        _require_positive("max_messages", max_messages)
        if block_seconds < 0:
            raise ValueError(f"block_seconds must not be negative, not {block_seconds}")
        deliveries: list[QueueDelivery] = []
        while self._new and len(deliveries) < max_messages:
            deliveries.append(self._deliver(self._new.popleft()))
        return deliveries

    async def ack(self, delivery: QueueDelivery) -> None:
        if self._pending.pop(delivery.delivery_id, None) is not None:
            self.acknowledged.append(delivery.delivery_id)

    async def reclaim(
        self, consumer: str, *, min_idle_seconds: float, max_messages: int
    ) -> list[QueueDelivery]:
        _require_positive("max_messages", max_messages)
        if min_idle_seconds < 0:
            raise ValueError(f"min_idle_seconds must not be negative, not {min_idle_seconds}")
        idle_since = self._clock.now() - timedelta(seconds=min_idle_seconds)
        idle = [
            entry
            for entry in self._pending_in_order()
            if entry.delivered_at is not None and entry.delivered_at <= idle_since
        ]
        return [self._deliver(entry) for entry in idle[:max_messages]]

    async def dead_letter(self, delivery: QueueDelivery) -> None:
        if self._pending.pop(delivery.delivery_id, None) is not None:
            self.dead_letters.append(delivery)

    def _pending_in_order(self) -> list[_QueueEntry]:
        return sorted(self._pending.values(), key=lambda entry: entry.sequence)

    def _deliver(self, entry: _QueueEntry) -> QueueDelivery:
        entry.delivery_count += 1
        entry.delivered_at = self._clock.now()
        self._pending[entry.delivery_id] = entry
        return self._delivery(entry)

    @staticmethod
    def _delivery(entry: _QueueEntry) -> QueueDelivery:
        return QueueDelivery(entry.delivery_id, entry.comment, entry.delivery_count)


# --- Services around a comment ---------------------------------------------------------


def ok_receipt(
    product_code: str,
    variant: str = "",
    quantity: int = 1,
    confidence: Decimal = Decimal("0.90"),
) -> FallbackReceipt:
    """An ``ok`` receipt proposing one product, for ``FakeIntentFallback``."""
    return FallbackReceipt(
        LlmOutcome.OK,
        LlmProposal(product_code, variant, quantity, confidence),
        request_id="fake-request",
        model="fake-model",
    )


class FakeIntentFallback:
    """Plays back scripted receipts, one per call, and records every request.

    Running out of receipts fails the test, as ``SequenceRandomSource`` does.
    With ``delay_seconds``, a call takes that long, but never longer than the
    request's timeout: at the timeout it returns a ``timeout`` receipt, as the
    port promises, and the scripted receipt stays for the next call.
    """

    def __init__(
        self, receipts: Iterable[FallbackReceipt] = (), *, delay_seconds: float = 0.0
    ) -> None:
        self._receipts = deque(receipts)
        self.delay_seconds = delay_seconds
        self.requests: list[FallbackRequest] = []

    def script(self, *receipts: FallbackReceipt) -> None:
        self._receipts.extend(receipts)

    async def resolve(self, request: FallbackRequest) -> FallbackReceipt:
        self.requests.append(request)
        if self.delay_seconds >= request.timeout_seconds:
            await asyncio.sleep(request.timeout_seconds)
            return FallbackReceipt(LlmOutcome.TIMEOUT)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if not self._receipts:
            raise AssertionError("FakeIntentFallback has no scripted receipt left")
        return self._receipts.popleft()


class FakeEventPublisher(_ScriptedFailures):
    """Records every published event, in order."""

    def __init__(self) -> None:
        self.published: list[SessionEvent] = []

    async def publish(self, event: SessionEvent) -> None:
        self._maybe_fail("publish")
        self.published.append(event)

    def of_type(self, event_type: SessionEventType) -> list[SessionEvent]:
        return [event for event in self.published if event.type is event_type]


class FakeBuyerNotifier(_ScriptedFailures):
    """Records every notification sent, in order."""

    def __init__(self) -> None:
        self.sent: list[BuyerNotification] = []

    async def notify(self, notification: BuyerNotification) -> None:
        self._maybe_fail("notify")
        self.sent.append(notification)


# --- Payment and shipping providers ----------------------------------------------------


class FakePaymentProvider(_ScriptedFailures):
    """Hosted checkout sessions kept in memory, and webhooks signed by an HMAC of the body.

    Like Stripe, it returns the same session for a reused idempotency key and
    refuses one reused with other parameters. ``complete`` pays a session,
    and ``signed_webhook`` builds a webhook this provider verifies.
    """

    def __init__(
        self,
        name: PaymentProviderName = PaymentProviderName.MOCK,
        *,
        key: bytes = b"fake-payment-key",
    ) -> None:
        self._name = name
        self._key = key
        self._sessions: dict[str, CheckoutSession] = {}
        self._by_key: dict[str, tuple[CheckoutRequest, str]] = {}
        self.requests: list[CheckoutRequest] = []

    @property
    def name(self) -> PaymentProviderName:
        return self._name

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        self._maybe_fail("create_checkout")
        self.requests.append(request)
        known = self._by_key.get(request.idempotency_key)
        if known is not None:
            first, provider_ref = known
            if first != request:
                raise ValueError(f"key {request.idempotency_key} reused with other parameters")
            return self._sessions[provider_ref]
        provider_ref = f"fake_cs_{len(self._sessions) + 1}"
        session = CheckoutSession(
            provider_ref,
            f"https://payments.invalid/checkout/{provider_ref}",
            CheckoutStatus.OPEN,
            request.amount,
        )
        self._sessions[provider_ref] = session
        self._by_key[request.idempotency_key] = (request, provider_ref)
        return session

    async def expire_checkout(self, provider_ref: str) -> bool:
        self._maybe_fail("expire_checkout")
        session = self._session(provider_ref)
        if session.status is not CheckoutStatus.OPEN:
            return False
        self._sessions[provider_ref] = replace(session, status=CheckoutStatus.EXPIRED)
        return True

    async def get_checkout(self, provider_ref: str) -> CheckoutSession:
        self._maybe_fail("get_checkout")
        return self._session(provider_ref)

    async def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> PaymentEvent:
        _check_signature(headers, raw_body, self._key)
        return PaymentEvent.model_validate_json(raw_body)

    def complete(self, provider_ref: str, *, payment_ref: str = "fake_pi") -> CheckoutSession:
        """The buyer pays an open session."""
        session = self._session(provider_ref)
        if session.status is not CheckoutStatus.OPEN:
            raise ValueError(f"session {provider_ref} is {session.status}, not open")
        completed = replace(session, status=CheckoutStatus.COMPLETE, payment_ref=payment_ref)
        self._sessions[provider_ref] = completed
        return completed

    def signed_webhook(self, event: PaymentEvent) -> tuple[dict[str, str], bytes]:
        body = event.model_dump_json().encode()
        return {FAKE_SIGNATURE_HEADER: _sign(self._key, body)}, body

    def _session(self, provider_ref: str) -> CheckoutSession:
        session = self._sessions.get(provider_ref)
        if session is None:
            raise RecordNotFound(f"no checkout session {provider_ref}")
        return session


class FakeCarrierProvider(_ScriptedFailures):
    """Shipments kept in memory, one per order; ``advance`` moves one along its machine."""

    def __init__(self, clock: Clock, name: CarrierName = CarrierName.MOCK) -> None:
        self._clock = clock
        self._name = name
        self._by_order: dict[UUID, ShipmentRef] = {}
        self._tracking: dict[str, TrackingUpdate] = {}
        self.requests: list[ShipmentRequest] = []

    @property
    def name(self) -> CarrierName:
        return self._name

    async def create_shipment(self, request: ShipmentRequest) -> ShipmentRef:
        self._maybe_fail("create_shipment")
        self.requests.append(request)
        known = self._by_order.get(request.order_id)
        if known is not None:
            return known
        number = len(self._by_order) + 1
        ref = ShipmentRef(self._name, f"fake-shipment-{number}", f"FAKE{number:08d}", self._now())
        self._by_order[request.order_id] = ref
        self._tracking[ref.carrier_ref] = TrackingUpdate(ShipmentStatus.CREATED, ref.created_at)
        return ref

    async def get_tracking(self, ref: ShipmentRef) -> TrackingUpdate:
        self._maybe_fail("get_tracking")
        return self._current(ref)

    async def cancel_shipment(self, ref: ShipmentRef) -> None:
        self._maybe_fail("cancel_shipment")
        self.advance(ref, ShipmentStatus.CANCELLED)

    def advance(self, ref: ShipmentRef, status: ShipmentStatus) -> TrackingUpdate:
        """The carrier moves the shipment: one allowed step, or ``InvalidTransition``."""
        current = self._current(ref)
        if transition(SHIPMENT_TRANSITIONS, current.status, status) is TransitionOutcome.NOOP:
            return current
        update = TrackingUpdate(status, self._now())
        self._tracking[ref.carrier_ref] = update
        return update

    def _current(self, ref: ShipmentRef) -> TrackingUpdate:
        update = self._tracking.get(ref.carrier_ref)
        if update is None:
            raise RecordNotFound(f"no shipment {ref.carrier_ref}")
        return update

    def _now(self) -> datetime:
        return self._clock.now()


# --- Repositories ----------------------------------------------------------------------


@dataclass(slots=True)
class InMemoryTables:
    """The rows of every table, by id, in creation order."""

    comments: dict[UUID, InboxComment] = field(default_factory=dict)
    sessions: dict[UUID, LiveSessionRecord] = field(default_factory=dict)
    products: dict[UUID, ProductRecord] = field(default_factory=dict)
    intents: dict[UUID, PurchaseIntentRecord] = field(default_factory=dict)
    orders: dict[UUID, OrderRecord] = field(default_factory=dict)
    payments: dict[UUID, PaymentRecord] = field(default_factory=dict)
    shipments: dict[UUID, ShipmentRecord] = field(default_factory=dict)
    webhook_events: dict[tuple[PaymentProviderName, str], RegisteredEvent] = field(
        default_factory=dict
    )


class InMemoryDatabase:
    """One database behind all the in-memory repositories, with helpers to seed it.

    ``random_source`` draws the order references, as in the SQL adapter: pass
    a ``SequenceRandomSource`` to force a collision.
    """

    def __init__(self, *, random_source: RandomSource = secrets) -> None:
        self.random_source = random_source
        self.tables = InMemoryTables()
        self._last_id = 0
        self.comments = InMemoryCommentInbox(self)
        self.live_sessions = InMemoryLiveSessionRepository(self)
        self.products = InMemoryProductStockRepository(self)
        self.orders = InMemoryOrderRepository(self)
        self.payments = InMemoryPaymentRepository(self)
        self.shipments = InMemoryShipmentRepository(self)
        self.webhook_events = InMemoryWebhookEventRegistry(self)

    def new_id(self) -> UUID:
        """Sequential ids, so that "lowest id first" means "created first", as with UUID v7."""
        self._last_id += 1
        return UUID(int=self._last_id)

    def add_live_session(
        self,
        *,
        external_live_id: str = "live-1",
        platform: Platform = Platform.SIMULATOR,
        status: LiveSessionStatus = LiveSessionStatus.LIVE,
        title: str = "Friday drop",
        seller_id: UUID | None = None,
    ) -> LiveSessionRecord:
        if any(
            known.platform is platform and known.external_live_id == external_live_id
            for known in self.tables.sessions.values()
        ):
            raise ValueError(f"{platform}/{external_live_id} is taken by another session")
        session = LiveSessionRecord(
            id=self.new_id(),
            seller_id=seller_id if seller_id is not None else self.new_id(),
            title=title,
            platform=platform,
            external_live_id=external_live_id,
            status=status,
        )
        self.tables.sessions[session.id] = session
        return session

    def add_product(
        self,
        session_id: UUID,
        code: str,
        *,
        variant: str = "",
        name: str = "Produit",
        price: Money | None = None,
        stock_total: int = 10,
    ) -> ProductRecord:
        if session_id not in self.tables.sessions:
            raise RecordNotFound(f"no live session {session_id}")
        product_code, product_variant = ProductCode(code), Variant(variant)
        rows = [
            row
            for row in self.tables.products.values()
            if row.session_id == session_id and row.code == product_code
        ]
        if any(row.variant == product_variant for row in rows):
            raise ValueError(f"{code} {variant!r} is already in the session")
        # A code has one row without variant, or only rows with one: the catalog
        # has no way to show both.
        if rows and (product_variant.is_empty or rows[0].variant.is_empty):
            raise ValueError(f"{code} cannot mix a row without variant and rows with one")
        product = ProductRecord(
            id=self.new_id(),
            session_id=session_id,
            code=product_code,
            variant=product_variant,
            name=name,
            price=price if price is not None else Money(1_000, "EUR"),
            stock=StockLevel(stock_total),
        )
        self.tables.products[product.id] = product
        return product

    def add_catalog(
        self,
        session_id: UUID,
        catalog: Catalog,
        *,
        price: Money | None = None,
        stock_total: int = 10,
    ) -> list[ProductRecord]:
        """One product row per code and variant of ``catalog``, such as ``demo_catalog()``."""
        return [
            self.add_product(
                session_id,
                str(product.code),
                variant=str(variant),
                name=product.name,
                price=price,
                stock_total=stock_total,
            )
            for product in catalog
            for variant in product.variants or (Variant(),)
        ]

    def find_product(
        self, session_id: UUID, code: ProductCode, variant: Variant
    ) -> ProductRecord | None:
        return next(
            (
                row
                for row in self.tables.products.values()
                if row.session_id == session_id and row.code == code and row.variant == variant
            ),
            None,
        )


class _Repository:
    def __init__(self, database: InMemoryDatabase) -> None:
        self._db = database

    @property
    def _tables(self) -> InMemoryTables:
        return self._db.tables


def _row[K, V](rows: Mapping[K, V], key: K, what: str) -> V:
    row = rows.get(key)
    if row is None:
        raise RecordNotFound(f"no {what} {key}")
    return row


class InMemoryCommentInbox(_Repository):
    async def claim(
        self, comment: QueuedComment, *, consumer: str, now: datetime, lease: timedelta
    ) -> CommentClaim:
        _require_aware(now)
        _require_positive("lease", lease.total_seconds())
        incoming = comment.comment
        known = self._find(incoming.platform, incoming.external_comment_id)
        if known is None:
            row = InboxComment(
                id=self._db.new_id(),
                platform=incoming.platform,
                external_comment_id=incoming.external_comment_id,
                author_platform_user_id=incoming.author_platform_user_id,
                text=incoming.text,
                correlation_id=comment.correlation_id,
                status=CommentStatus.RECEIVED,
                created_at=now,
                claimed_by=consumer,
                claimed_until=now + lease,
            )
        elif known.status is CommentStatus.RECEIVED and (
            known.claimed_until is None or known.claimed_until < now
        ):
            row = replace(known, claimed_by=consumer, claimed_until=now + lease)
        else:
            return CommentClaim(acquired=False, comment=known)
        self._tables.comments[row.id] = row
        return CommentClaim(acquired=True, comment=row)

    async def get(self, comment_id: UUID) -> InboxComment | None:
        return self._tables.comments.get(comment_id)

    async def record_llm_receipt(
        self, comment_id: UUID, receipt: FallbackReceipt
    ) -> FallbackReceipt:
        row = _row(self._tables.comments, comment_id, "comment")
        if row.llm_receipt is not None:
            return row.llm_receipt
        self._tables.comments[comment_id] = replace(row, llm_receipt=receipt)
        return receipt

    async def reject(
        self, comment_id: UUID, reason: RejectionReason, *, session_id: UUID | None
    ) -> TransitionOutcome:
        row = _row(self._tables.comments, comment_id, "comment")
        outcome = transition(COMMENT_TRANSITIONS, row.status, CommentStatus.REJECTED)
        if outcome is TransitionOutcome.APPLIED:
            self._tables.comments[comment_id] = replace(
                row, status=CommentStatus.REJECTED, rejection_reason=reason, session_id=session_id
            )
        return outcome

    async def dead_letter(self, platform: Platform, external_comment_id: str) -> TransitionOutcome:
        row = self._find(platform, external_comment_id)
        if row is None:
            raise RecordNotFound(f"no comment {platform}/{external_comment_id}")
        outcome = transition(COMMENT_TRANSITIONS, row.status, CommentStatus.DEAD_LETTERED)
        if outcome is TransitionOutcome.APPLIED:
            self._tables.comments[row.id] = replace(row, status=CommentStatus.DEAD_LETTERED)
        return outcome

    def _find(self, platform: Platform, external_comment_id: str) -> InboxComment | None:
        return next(
            (
                row
                for row in self._tables.comments.values()
                if row.platform is platform and row.external_comment_id == external_comment_id
            ),
            None,
        )


class InMemoryLiveSessionRepository(_Repository):
    async def get(self, session_id: UUID) -> LiveSessionRecord | None:
        return self._tables.sessions.get(session_id)

    async def find_by_live_id(
        self, platform: Platform, external_live_id: str
    ) -> LiveSessionRecord | None:
        return next(
            (
                session
                for session in self._tables.sessions.values()
                if session.platform is platform and session.external_live_id == external_live_id
            ),
            None,
        )

    async def load_catalog(self, session_id: UUID) -> Catalog:
        _row(self._tables.sessions, session_id, "live session")
        by_code: dict[ProductCode, list[ProductRecord]] = {}
        for row in self._tables.products.values():
            if row.session_id == session_id:
                by_code.setdefault(row.code, []).append(row)
        return Catalog(
            tuple(
                CatalogProduct(
                    code,
                    rows[0].name,
                    tuple(row.variant for row in rows if not row.variant.is_empty),
                )
                for code, rows in by_code.items()
            )
        )

    async def set_status(
        self, session_id: UUID, target: LiveSessionStatus, *, now: datetime
    ) -> TransitionOutcome:
        _require_aware(now)
        session = _row(self._tables.sessions, session_id, "live session")
        outcome = transition(LIVE_SESSION_TRANSITIONS, session.status, target)
        if outcome is TransitionOutcome.APPLIED:
            # Only draft -> live and live -> ended exist.
            if target is LiveSessionStatus.LIVE:
                session = replace(session, status=target, started_at=now)
            else:
                session = replace(session, status=target, ended_at=now)
            self._tables.sessions[session_id] = session
        return outcome


class InMemoryProductStockRepository(_Repository):
    async def get(self, product_id: UUID) -> ProductRecord | None:
        return self._tables.products.get(product_id)

    async def list_for_session(self, session_id: UUID) -> list[ProductRecord]:
        return [row for row in self._tables.products.values() if row.session_id == session_id]

    async def restock(self, product_id: UUID, delta: int) -> ProductRecord:
        product = _row(self._tables.products, product_id, "product")
        stock = product.stock
        total = stock.total + delta
        if total < stock.reserved + stock.sold:
            raise StockConflict(
                f"product {product_id}: a total of {total} is below the "
                f"{stock.reserved} reserved and {stock.sold} sold"
            )
        product = replace(product, stock=replace(stock, total=total))
        self._tables.products[product_id] = product
        return product


class InMemoryOrderRepository(_Repository):
    async def reserve(self, request: ReservationRequest) -> ReservationResult | None:
        comment = _row(self._tables.comments, request.comment_id, "comment")
        if comment.status is not CommentStatus.RECEIVED:
            return None
        session = _row(self._tables.sessions, request.session_id, "live session")
        product = self._db.find_product(session.id, request.product_code, request.variant)
        if product is None:
            raise RecordNotFound(
                f"no product {request.product_code} {request.variant.value!r} in {session.id}"
            )
        quantity = request.quantity.value
        buyer = comment.author_platform_user_id
        if session.status is not LiveSessionStatus.LIVE or product.stock.available < quantity:
            outcome = IntentOutcome.OUT_OF_STOCK
        elif self._pending_order(session.id, product.id, buyer) is not None:
            outcome = IntentOutcome.ALREADY_RESERVED
        else:
            outcome = IntentOutcome.RESERVED
        # Every check and draw comes before the first write, so that an error
        # leaves the tables as they were, like a rolled-back transaction.
        reference = self._draw_reference() if outcome is IntentOutcome.RESERVED else None
        intent = PurchaseIntentRecord(
            id=self._db.new_id(),
            comment_id=comment.id,
            session_id=session.id,
            product_id=product.id,
            quantity=request.quantity,
            source=request.source,
            confidence=request.confidence,
            evidence=request.evidence,
            outcome=outcome,
        )
        order = None
        stock = product.stock
        if reference is not None:
            stock = replace(stock, reserved=stock.reserved + quantity)
            order = OrderRecord(
                id=self._db.new_id(),
                reference=reference,
                seller_id=session.seller_id,
                session_id=session.id,
                intent_id=intent.id,
                product_id=product.id,
                quantity=request.quantity,
                unit_price=product.price,
                total=product.price * quantity,
                status=OrderStatus.PENDING_PAYMENT,
                reserved_until=request.reserved_until,
                buyer_platform_user_id=buyer,
                created_at=request.now,
            )
            self._tables.orders[order.id] = order
            self._tables.products[product.id] = replace(product, stock=stock)
        self._tables.comments[comment.id] = replace(
            comment, status=CommentStatus.PROCESSED, session_id=session.id
        )
        self._tables.intents[intent.id] = intent
        return ReservationResult(intent, order, stock)

    async def find_reservation(self, comment_id: UUID) -> ReservationResult | None:
        intent = next(
            (row for row in self._tables.intents.values() if row.comment_id == comment_id), None
        )
        if intent is None:
            return None
        order = next(
            (row for row in self._tables.orders.values() if row.intent_id == intent.id), None
        )
        return ReservationResult(intent, order, self._tables.products[intent.product_id].stock)

    async def get(self, order_id: UUID) -> OrderRecord | None:
        return self._tables.orders.get(order_id)

    async def get_by_reference(self, reference: OrderReference) -> OrderRecord | None:
        return next(
            (row for row in self._tables.orders.values() if row.reference == reference), None
        )

    async def mark_buyer_notified(self, order_id: UUID, *, now: datetime) -> OrderRecord:
        _require_aware(now)
        order = _row(self._tables.orders, order_id, "order")
        if order.buyer_notified_at is None:
            order = replace(order, buyer_notified_at=now)
            self._tables.orders[order_id] = order
        return order

    async def set_shipping_address(self, order_id: UUID, address: ShippingAddress) -> OrderRecord:
        order = replace(_row(self._tables.orders, order_id, "order"), shipping_address=address)
        self._tables.orders[order_id] = order
        return order

    async def expire_due(self, *, now: datetime, limit: int) -> list[OrderRecord]:
        _require_aware(now)
        _require_positive("limit", limit)
        paying = {
            payment.order_id
            for payment in self._tables.payments.values()
            if payment.status is PaymentStatus.PENDING
        }
        due = sorted(
            (
                order
                for order in self._tables.orders.values()
                if order.status is OrderStatus.PENDING_PAYMENT
                and order.reserved_until < now
                and order.id not in paying
            ),
            key=lambda order: order.id,
        )[:limit]
        expired = []
        for order in due:
            transition(ORDER_TRANSITIONS, order.status, OrderStatus.EXPIRED)
            self._move_stock(order, reserved=-order.quantity.value)
            order = replace(order, status=OrderStatus.EXPIRED, expired_at=now)
            self._tables.orders[order.id] = order
            expired.append(order)
        return expired

    async def record_payment(self, order_id: UUID, *, now: datetime) -> OrderTransition:
        _require_aware(now)
        order = _row(self._tables.orders, order_id, "order")
        if order.status in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAID):
            target = OrderStatus.PAID
        else:
            target = OrderStatus.PAID_AFTER_EXPIRY
        outcome = transition(ORDER_TRANSITIONS, order.status, target)
        if outcome is TransitionOutcome.APPLIED:
            if target is OrderStatus.PAID:
                self._move_stock(order, reserved=-order.quantity.value, sold=order.quantity.value)
            order = replace(order, status=target, paid_at=now)
            self._tables.orders[order_id] = order
        return OrderTransition(order, outcome)

    def _pending_order(self, session_id: UUID, product_id: UUID, buyer: str) -> OrderRecord | None:
        return next(
            (
                order
                for order in self._tables.orders.values()
                if order.status is OrderStatus.PENDING_PAYMENT
                and order.session_id == session_id
                and order.product_id == product_id
                and order.buyer_platform_user_id == buyer
            ),
            None,
        )

    def _draw_reference(self) -> OrderReference:
        taken = {order.reference for order in self._tables.orders.values()}
        for _ in range(REFERENCE_DRAWS):
            reference = generate_order_reference(self._db.random_source)
            if reference not in taken:
                return reference
        raise ServiceUnavailable(f"{REFERENCE_DRAWS} order reference draws collided")

    def _move_stock(self, order: OrderRecord, *, reserved: int, sold: int = 0) -> None:
        product = self._tables.products[order.product_id]
        stock = product.stock
        moved = replace(stock, reserved=stock.reserved + reserved, sold=stock.sold + sold)
        self._tables.products[product.id] = replace(product, stock=moved)


class InMemoryPaymentRepository(_Repository):
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
        _require_aware(now)
        _require_positive("grace", grace.total_seconds())
        _require_positive("max_attempts", max_attempts)
        order = _row(self._tables.orders, order_id, "order")
        attempts = [row for row in self._tables.payments.values() if row.order_id == order_id]
        pending = next((row for row in attempts if row.status is PaymentStatus.PENDING), None)
        if pending is not None:
            return AttemptOpening(AttemptOpeningKind.PENDING, pending)
        if order.status is not OrderStatus.PENDING_PAYMENT or order.reserved_until <= now:
            return AttemptOpening(AttemptOpeningKind.NOT_PAYABLE)
        if len(attempts) >= max_attempts:
            return AttemptOpening(AttemptOpeningKind.EXHAUSTED)
        self._tables.orders[order_id] = replace(
            order, reserved_until=max(order.reserved_until, now + grace)
        )
        payment = PaymentRecord(
            id=self._db.new_id(),
            order_id=order_id,
            attempt_no=len(attempts) + 1,
            provider=provider,
            amount=order.total,
            return_url=return_url,
            status=PaymentStatus.PENDING,
            created_at=now,
        )
        self._tables.payments[payment.id] = payment
        return AttemptOpening(AttemptOpeningKind.OPENED, payment)

    async def record_checkout(
        self, payment_id: UUID, *, provider_ref: str, redirect_url: str
    ) -> PaymentRecord:
        payment = _row(self._tables.payments, payment_id, "payment")
        if payment.provider_ref is None:
            payment = replace(payment, provider_ref=provider_ref, redirect_url=redirect_url)
            self._tables.payments[payment_id] = payment
        return payment

    async def get_pending(self, order_id: UUID) -> PaymentRecord | None:
        return next(
            (
                row
                for row in self._tables.payments.values()
                if row.order_id == order_id and row.status is PaymentStatus.PENDING
            ),
            None,
        )

    async def find_by_provider_ref(self, provider_ref: str) -> PaymentRecord | None:
        return next(
            (row for row in self._tables.payments.values() if row.provider_ref == provider_ref),
            None,
        )

    async def set_status(
        self, payment_id: UUID, target: PaymentStatus, *, provider_payment_ref: str | None = None
    ) -> TransitionOutcome:
        payment = _row(self._tables.payments, payment_id, "payment")
        outcome = transition(PAYMENT_TRANSITIONS, payment.status, target)
        if outcome is TransitionOutcome.APPLIED:
            self._tables.payments[payment_id] = replace(
                payment,
                status=target,
                provider_payment_ref=(
                    provider_payment_ref
                    if provider_payment_ref is not None
                    else payment.provider_payment_ref
                ),
            )
        return outcome


class InMemoryShipmentRepository(_Repository):
    async def add(self, order_id: UUID, ref: ShipmentRef) -> ShipmentRecord:
        _row(self._tables.orders, order_id, "order")
        known = self._find_by_order(order_id)
        if known is not None:
            return known
        shipment = ShipmentRecord(self._db.new_id(), order_id, ref, ShipmentStatus.CREATED)
        self._tables.shipments[shipment.id] = shipment
        return shipment

    async def get_by_order(self, order_id: UUID) -> ShipmentRecord | None:
        return self._find_by_order(order_id)

    def _find_by_order(self, order_id: UUID) -> ShipmentRecord | None:
        # Synchronous, so that add() checks and inserts without an await between.
        return next(
            (row for row in self._tables.shipments.values() if row.order_id == order_id), None
        )

    async def list_in_flight(self, *, limit: int) -> list[ShipmentRecord]:
        _require_positive("limit", limit)
        settled = (ShipmentStatus.DELIVERED, ShipmentStatus.CANCELLED)
        in_flight = [row for row in self._tables.shipments.values() if row.status not in settled]
        return sorted(in_flight, key=lambda row: row.id)[:limit]

    async def set_status(
        self, shipment_id: UUID, target: ShipmentStatus, *, event_at: datetime
    ) -> TransitionOutcome:
        _require_aware(event_at)
        shipment = _row(self._tables.shipments, shipment_id, "shipment")
        outcome = transition(SHIPMENT_TRANSITIONS, shipment.status, target)
        if outcome is TransitionOutcome.APPLIED:
            self._tables.shipments[shipment_id] = replace(
                shipment, status=target, last_event_at=event_at
            )
        return outcome


class InMemoryWebhookEventRegistry(_Repository):
    async def record(self, event: PaymentEvent, *, now: datetime) -> bool:
        _require_aware(now)
        key = (event.provider, event.event_id)
        known = self._tables.webhook_events.get(key)
        if known is None:
            self._tables.webhook_events[key] = RegisteredEvent(event, received_at=now)
            return True
        self._tables.webhook_events[key] = replace(known, delivery_count=known.delivery_count + 1)
        return False

    async def pending(self, *, limit: int) -> list[RegisteredEvent]:
        _require_positive("limit", limit)
        unprocessed = [
            row for row in self._tables.webhook_events.values() if row.processed_at is None
        ]
        # A stable sort: events received at the same instant keep their arrival order.
        return sorted(unprocessed, key=lambda row: row.received_at)[:limit]

    async def mark_processed(
        self,
        provider: PaymentProviderName,
        event_id: str,
        outcome: WebhookOutcome,
        *,
        now: datetime,
    ) -> bool:
        _require_aware(now)
        key = (provider, event_id)
        registered = _row(self._tables.webhook_events, key, "webhook event")
        if registered.processed_at is not None:
            return False
        self._tables.webhook_events[key] = replace(registered, processed_at=now, outcome=outcome)
        return True

    async def record_failure(
        self, provider: PaymentProviderName, event_id: str, *, max_attempts: int, now: datetime
    ) -> RegisteredEvent:
        _require_aware(now)
        _require_positive("max_attempts", max_attempts)
        key = (provider, event_id)
        registered = _row(self._tables.webhook_events, key, "webhook event")
        if registered.processed_at is not None:
            return registered
        attempts = registered.attempts + 1
        if attempts >= max_attempts:
            registered = replace(
                registered, attempts=attempts, processed_at=now, outcome=WebhookOutcome.FAILED
            )
        else:
            registered = replace(registered, attempts=attempts)
        self._tables.webhook_events[key] = registered
        return registered
