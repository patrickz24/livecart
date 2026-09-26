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
import contextlib
import hashlib
import hmac
import math
import secrets
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from livecart_core.application.dto import (
    MAX_STOCK_TOTAL,
    AttemptOpening,
    AttemptOpeningKind,
    BuyerNotification,
    BuyerOrderRecord,
    CarrierName,
    CheckoutRequest,
    CheckoutSession,
    CheckoutStatus,
    CommentBatch,
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
    PurchaseIntentRecord,
    QueuedComment,
    QueueDelivery,
    RateLimitKey,
    RecordedIntent,
    RefreshTokenRecord,
    RegisteredEvent,
    RejectedComment,
    ReservationRequest,
    ReservationResult,
    SellerRecord,
    SessionActivity,
    SessionEvent,
    SessionEventType,
    ShipmentRecord,
    ShipmentRef,
    ShipmentRequest,
    ShippingAddress,
    StockLevel,
    StreamedEvent,
    TrackingUpdate,
    WebhookOutcome,
    WindowCount,
    event_id_key,
    page_of,
)
from livecart_core.application.ports import (
    REFERENCE_DRAWS,
    START_CURSOR,
    Clock,
    InvalidSignature,
    LiveSessionConflict,
    ProductConflict,
    RecordNotFound,
    SellerConflict,
    ServiceUnavailable,
    StockConflict,
)
from livecart_core.domain.catalog import Catalog, CatalogProduct
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.live_sessions import (
    MAX_PRODUCTS_PER_SESSION,
    CatalogConflict,
    check_session_edit,
    variant_conflict,
)
from livecart_core.domain.llm_decision import LlmProposal
from livecart_core.domain.money import Money
from livecart_core.domain.outcomes import IntentOutcome, LlmOutcome, LlmReason, RejectionReason
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
from livecart_core.infrastructure.events import stream_gap
from livecart_core.order_tokens import OrderTokens
from livecart_core.testing.keys import TEST_ORDER_TOKEN_KEY

# The day milestone 2 landed, at noon UTC: any fixed instant would do.
FAKE_CLOCK_START: Final = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
# Before every time a fake records: what an absent time sorts as.
_EARLIEST: Final = datetime.min.replace(tzinfo=UTC)
# The access tokens of the orders the fakes open, under the key of the tests.
TEST_ORDER_TOKENS: Final = OrderTokens(TEST_ORDER_TOKEN_KEY)
# No password verifies against it, as with the "!" of a locked account in /etc/shadow.
LOCKED_ACCOUNT: Final = "!"
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


# --- Seller authentication -------------------------------------------------------------

FAKE_HASH_PREFIX: Final = "fake-hash:"
# What FakePasswordHasher checks the password of an unknown or locked account against.
FAKE_DUMMY_HASH: Final = f"{FAKE_HASH_PREFIX}dummy"


class FakePasswordHasher:
    """Instant, deterministic and plainly fake hashes: ``fake-hash:`` then the password's SHA-256.

    ``verified`` records the hash each ``verify`` checked, ``FAKE_DUMMY_HASH``
    for an unknown account, so that a test can prove that an unknown e-mail
    and a wrong password take the same path. A stored value without the
    prefix stands for a locked account, as ``!`` does in the seeds.
    """

    def __init__(self) -> None:
        self.verified: list[str] = []

    @staticmethod
    def hash_of(password: str) -> str:
        return f"{FAKE_HASH_PREFIX}{hashlib.sha256(password.encode()).hexdigest()}"

    async def hash(self, password: str) -> str:
        return self.hash_of(password)

    async def verify(self, password_hash: str | None, password: str) -> bool:
        if password_hash is None or not password_hash.startswith(FAKE_HASH_PREFIX):
            self.verified.append(FAKE_DUMMY_HASH)
            return False
        self.verified.append(password_hash)
        return hmac.compare_digest(password_hash, self.hash_of(password))


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
    once. Idle time is measured on the clock it is given. It keeps no settled
    history, so ``trim`` has nothing to drop.
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

    async def trim(self) -> int:
        return 0

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
    product_code: str | None,
    variant: str | None = None,
    quantity: int | None = 1,
    confidence: Decimal = Decimal("0.90"),
    *,
    reason: LlmReason = LlmReason.ORDER,
) -> FallbackReceipt:
    """An ``ok`` receipt, for ``FakeIntentFallback``: an order of one product by default.

    ``ok_receipt(None, quantity=None, reason=LlmReason.NO_ORDER)`` is a model
    that read no order.
    """
    return FallbackReceipt(
        LlmOutcome.OK,
        LlmProposal(reason, product_code, variant, quantity, confidence),
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


@dataclass(frozen=True, slots=True)
class _StreamEntry:
    key: tuple[int, int]
    event: SessionEvent
    appended_at: datetime

    @property
    def streamed(self) -> StreamedEvent:
        return StreamedEvent(f"{self.key[0]}-{self.key[1]}", self.event)


@dataclass
class _EventStream:
    entries: list[_StreamEntry] = field(default_factory=list)
    last: tuple[int, int] = (0, 0)
    added: int = 0


class InMemorySessionEvents(_ScriptedFailures):
    """The session streams in memory, with the rules of their Redis adapter.

    Both ports of the stream: ``EventPublisher`` and ``SessionEventStream``.
    Ids are ``<n>-0``, ``n`` counting every event this fake appended, so that
    they increase across streams as Redis's do. Each publication drops the
    events of its stream older than ``retention`` on the fake's clock, as
    ``XADD ... MINID`` does; ``expire`` drops a whole stream, as its time to
    live would. ``read`` waits, in real time, up to its block time for an
    event to come. ``published`` lists every event appended, in order.
    """

    def __init__(self, clock: Clock, *, retention: timedelta = timedelta(minutes=15)) -> None:
        self._clock = clock
        self._retention = retention
        self._streams: dict[UUID, _EventStream] = {}
        self._sequence = 0
        # Set, then replaced, on every publication: what a blocked read waits on.
        self._appended = asyncio.Event()
        self.published: list[SessionEvent] = []

    async def publish(self, event: SessionEvent) -> None:
        self._maybe_fail("publish")
        now = self._clock.now()
        stream = self._streams.setdefault(event.session_id, _EventStream())
        kept = [entry for entry in stream.entries if entry.appended_at >= now - self._retention]
        self._sequence += 1
        stream.last = (self._sequence, 0)
        stream.added += 1
        stream.entries = [*kept, _StreamEntry(stream.last, event, now)]
        self.published.append(event)
        self._appended.set()
        self._appended = asyncio.Event()

    async def cursor(self, session_id: UUID) -> str:
        self._maybe_fail("cursor")
        stream = self._streams.get(session_id)
        if stream is None or not stream.entries:
            return START_CURSOR
        return stream.entries[-1].streamed.event_id

    async def read(
        self, session_id: UUID, *, after: str, max_events: int, block_seconds: float
    ) -> list[StreamedEvent]:
        cursor = event_id_key(after)
        if max_events < 1 or block_seconds < 0:
            raise ValueError("a read takes one event at least, and waits zero seconds or more")
        self._maybe_fail("read")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + block_seconds
        while not (found := self._after(session_id, cursor, max_events)):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(remaining):
                    await self._appended.wait()
        return found

    async def has_gap(self, session_id: UUID, *, after: str) -> bool:
        cursor = event_id_key(after)
        self._maybe_fail("has_gap")
        stream = self._streams.get(session_id)
        if stream is None:
            return True
        return stream_gap(
            cursor,
            last=stream.last,
            first=stream.entries[0].key if stream.entries else (0, 0),
            length=len(stream.entries),
            added=stream.added,
        )

    def expire(self, session_id: UUID) -> None:
        """Drop the session's stream, as Redis does once its time to live runs out."""
        self._streams.pop(session_id, None)

    def of_type(self, event_type: SessionEventType) -> list[SessionEvent]:
        return [event for event in self.published if event.type is event_type]

    def _after(self, session_id: UUID, cursor: tuple[int, int], limit: int) -> list[StreamedEvent]:
        stream = self._streams.get(session_id)
        entries = [] if stream is None else stream.entries
        return [entry.streamed for entry in entries if entry.key > cursor][:limit]


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


# --- Idempotency -----------------------------------------------------------------------


class InMemoryIdempotencyStore(_ScriptedFailures):
    """The store of idempotent answers, in a dict; claims never expire, where Redis's last a day."""

    def __init__(self) -> None:
        self.records: dict[IdempotencyKey, IdempotencyRecord] = {}

    async def claim(self, key: IdempotencyKey, fingerprint: str) -> IdempotencyRecord | None:
        self._maybe_fail("claim")
        first = self.records.get(key)
        if first is None:
            self.records[key] = IdempotencyRecord(fingerprint=fingerprint)
        return first

    async def save(self, key: IdempotencyKey, record: IdempotencyRecord) -> None:
        self._maybe_fail("save")
        # SET XX: an answer never recreates a key that was released meanwhile.
        if key in self.records:
            self.records[key] = record

    async def release(self, key: IdempotencyKey) -> None:
        self._maybe_fail("release")
        self.records.pop(key, None)


# --- Rate limiting ---------------------------------------------------------------------


def _whole_seconds_until(end: datetime, now: datetime) -> int:
    """Seconds from ``now`` to ``end``, rounded up, as Retry-After counts them; 0 once past."""
    return max(0, math.ceil((end - now).total_seconds()))


class InMemoryRateLimiter(_ScriptedFailures):
    """Fixed windows in a dict, timed by ``clock``.

    A window ends ``window_seconds`` after its first hit, as in Redis.
    ``windows`` holds each running window's hits and end, for the tests to read.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.windows: dict[RateLimitKey, tuple[int, datetime]] = {}

    def _running(self, key: RateLimitKey, now: datetime) -> tuple[int, datetime] | None:
        window = self.windows.get(key)
        if window is not None and window[1] <= now:
            # Redis expires the key at the end of its window: the next hit starts afresh.
            del self.windows[key]
            return None
        return window

    async def hit(self, key: RateLimitKey, *, window_seconds: int) -> WindowCount:
        self._maybe_fail("hit")
        _require_positive("window_seconds", window_seconds)
        now = self._clock.now()
        hits, end = self._running(key, now) or (0, now + timedelta(seconds=window_seconds))
        self.windows[key] = (hits + 1, end)
        return WindowCount(hits + 1, _whole_seconds_until(end, now))

    async def peek(self, key: RateLimitKey) -> WindowCount:
        self._maybe_fail("peek")
        now = self._clock.now()
        window = self._running(key, now)
        if window is None:
            return WindowCount(0, 0)
        return WindowCount(window[0], _whole_seconds_until(window[1], now))


# --- Repositories ----------------------------------------------------------------------


@dataclass(slots=True)
class InMemoryTables:
    """The rows of every table, by id, in creation order."""

    sellers: dict[UUID, SellerRecord] = field(default_factory=dict)
    refresh_tokens: dict[UUID, RefreshTokenRecord] = field(default_factory=dict)
    # refresh_tokens.token_hash, the unique column tokens are looked up by.
    refresh_token_ids: dict[bytes, UUID] = field(default_factory=dict)
    comments: dict[UUID, InboxComment] = field(default_factory=dict)
    sessions: dict[UUID, LiveSessionRecord] = field(default_factory=dict)
    products: dict[UUID, ProductRecord] = field(default_factory=dict)
    intents: dict[UUID, PurchaseIntentRecord] = field(default_factory=dict)
    orders: dict[UUID, OrderRecord] = field(default_factory=dict)
    # orders.access_token_hash, beside the records, which never carry it.
    order_token_hashes: dict[UUID, bytes] = field(default_factory=dict)
    payments: dict[UUID, PaymentRecord] = field(default_factory=dict)
    shipments: dict[UUID, ShipmentRecord] = field(default_factory=dict)
    webhook_events: dict[tuple[PaymentProviderName, str], RegisteredEvent] = field(
        default_factory=dict
    )


class InMemoryDatabase:
    """One database behind all the in-memory repositories, with helpers to seed it.

    ``random_source`` draws the order references, as in the SQL adapter: pass
    a ``SequenceRandomSource`` to force a collision. ``currency`` prices the
    products the repositories add, as ``DEFAULT_CURRENCY`` does in production;
    ``access_token_hash`` gives the stored hash of an order's token, derived
    from ``TEST_ORDER_TOKEN_KEY`` unless a test passes another derivation.
    """

    def __init__(
        self,
        *,
        random_source: RandomSource = secrets,
        currency: str = "EUR",
        access_token_hash: Callable[[UUID], bytes] = TEST_ORDER_TOKENS.stored_hash,
    ) -> None:
        self.random_source = random_source
        self.currency = currency
        self.access_token_hash = access_token_hash
        self.tables = InMemoryTables()
        self._last_id = 0
        self.sellers = InMemorySellerRepository(self)
        self.refresh_tokens = InMemoryRefreshTokenRepository(self)
        self.comments = InMemoryCommentInbox(self)
        self.live_sessions = InMemoryLiveSessionRepository(self)
        self.products = InMemoryProductStockRepository(self)
        self.orders = InMemoryOrderRepository(self)
        self.activity = InMemoryLiveSessionActivity(self)
        self.payments = InMemoryPaymentRepository(self)
        self.shipments = InMemoryShipmentRepository(self)
        self.webhook_events = InMemoryWebhookEventRegistry(self)

    def new_id(self) -> UUID:
        """Sequential ids, so that "lowest id first" means "created first", as with UUID v7."""
        self._last_id += 1
        return UUID(int=self._last_id)

    def add_seller(self, *, slug: str | None = None, display_name: str = "Seller") -> SellerRecord:
        """A seller whose account no password opens, as a seed would create one."""
        seller_id = self.new_id()
        slug = slug if slug is not None else f"seller-{seller_id.int}"
        seller = SellerRecord(
            id=seller_id,
            email=f"{slug}@example.test",
            slug=slug,
            display_name=display_name,
            password_hash=LOCKED_ACCOUNT,
            created_at=FAKE_CLOCK_START,
        )
        self.tables.sellers[seller.id] = seller
        return seller

    def add_live_session(
        self,
        *,
        external_live_id: str = "live-1",
        platform: Platform = Platform.SIMULATOR,
        status: LiveSessionStatus = LiveSessionStatus.LIVE,
        title: str = "Friday drop",
        seller_id: UUID | None = None,
    ) -> LiveSessionRecord:
        """A session of ``seller_id``, or of a seller without an account row: seeding only."""
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
            created_at=FAKE_CLOCK_START,
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

    def session_of(self, seller_id: UUID, session_id: UUID) -> LiveSessionRecord | None:
        """The session if ``seller_id`` owns it: the seller filter of every seller-scoped query."""
        session = self.tables.sessions.get(session_id)
        return session if session is not None and session.seller_id == seller_id else None

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


class InMemorySellerRepository(_Repository):
    async def add(
        self, *, email: str, slug: str, display_name: str, password_hash: str, now: datetime
    ) -> SellerRecord:
        _require_aware(now)
        # The order of the SQL checks does not matter: a scenario takes one field at a time.
        if self._find(email) is not None:
            raise SellerConflict("email")
        if any(row.slug == slug for row in self._tables.sellers.values()):
            raise SellerConflict("slug")
        seller = SellerRecord(
            id=self._db.new_id(),
            email=email,
            slug=slug,
            display_name=display_name,
            password_hash=password_hash,
            created_at=now,
        )
        self._tables.sellers[seller.id] = seller
        return seller

    async def get(self, seller_id: UUID) -> SellerRecord | None:
        return self._tables.sellers.get(seller_id)

    async def find_by_email(self, email: str) -> SellerRecord | None:
        return self._find(email)

    async def find_by_slug(self, slug: str) -> SellerRecord | None:
        return next((row for row in self._tables.sellers.values() if row.slug == slug), None)

    def _find(self, email: str) -> SellerRecord | None:
        # lower(), as the unique index uq_sellers_email: the e-mails are ASCII.
        return next(
            (row for row in self._tables.sellers.values() if row.email.lower() == email.lower()),
            None,
        )


class InMemoryRefreshTokenRepository(_Repository):
    async def issue(
        self, seller_id: UUID, token_hash: bytes, *, now: datetime, expires_at: datetime
    ) -> RefreshTokenRecord:
        _require_aware(now)
        _row(self._tables.sellers, seller_id, "seller")
        return self._store(
            RefreshTokenRecord(
                id=self._db.new_id(),
                seller_id=seller_id,
                family_id=self._db.new_id(),
                expires_at=_require_aware(expires_at),
                created_at=now,
            ),
            token_hash,
        )

    async def rotate(
        self, token_hash: bytes, successor_hash: bytes, *, now: datetime
    ) -> RefreshTokenRecord | None:
        _require_aware(now)
        current = self._find(token_hash)
        if current is None or not current.is_active(now):
            return None
        successor = self._store(
            RefreshTokenRecord(
                id=self._db.new_id(),
                seller_id=current.seller_id,
                family_id=current.family_id,
                expires_at=current.expires_at,
                created_at=now,
            ),
            successor_hash,
        )
        self._tables.refresh_tokens[current.id] = replace(
            current, used_at=now, replaced_by_id=successor.id
        )
        return successor

    async def find(self, token_hash: bytes) -> RefreshTokenRecord | None:
        return self._find(token_hash)

    async def get(self, token_id: UUID) -> RefreshTokenRecord | None:
        return self._tables.refresh_tokens.get(token_id)

    async def revoke_family(self, family_id: UUID, *, now: datetime) -> int:
        _require_aware(now)
        family = [
            row
            for row in self._tables.refresh_tokens.values()
            if row.family_id == family_id and row.revoked_at is None
        ]
        for row in family:
            self._tables.refresh_tokens[row.id] = replace(row, revoked_at=now)
        return len(family)

    def _find(self, token_hash: bytes) -> RefreshTokenRecord | None:
        token_id = self._tables.refresh_token_ids.get(token_hash)
        return None if token_id is None else self._tables.refresh_tokens[token_id]

    def _store(self, record: RefreshTokenRecord, token_hash: bytes) -> RefreshTokenRecord:
        # uq_refresh_tokens_token_hash; a 256-bit token never collides outside a test.
        if token_hash in self._tables.refresh_token_ids:
            raise ValueError("another refresh token has this hash")
        self._tables.refresh_tokens[record.id] = record
        self._tables.refresh_token_ids[token_hash] = record.id
        return record


class InMemoryCommentInbox(_Repository):
    async def claim(
        self, comment: QueuedComment, *, holder: str, now: datetime, lease: timedelta
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
                claimed_by=holder,
                claimed_until=now + lease,
            )
        elif known.status is CommentStatus.RECEIVED and (
            known.claimed_until is None or known.claimed_until < now or known.claimed_by == holder
        ):
            row = replace(known, claimed_by=holder, claimed_until=now + lease)
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
        self,
        comment_id: UUID,
        reason: RejectionReason,
        *,
        session_id: UUID | None,
        parse: ParseSummary | None = None,
    ) -> TransitionOutcome:
        row = _row(self._tables.comments, comment_id, "comment")
        outcome = transition(COMMENT_TRANSITIONS, row.status, CommentStatus.REJECTED)
        if outcome is TransitionOutcome.APPLIED:
            self._tables.comments[comment_id] = replace(
                row,
                status=CommentStatus.REJECTED,
                rejection_reason=reason,
                session_id=session_id,
                parse=parse if parse is not None else row.parse,
            )
        return outcome

    async def dead_letter(self, comment: QueuedComment, *, now: datetime) -> TransitionOutcome:
        _require_aware(now)
        incoming = comment.comment
        row = self._find(incoming.platform, incoming.external_comment_id)
        if row is None:
            row = InboxComment(
                id=self._db.new_id(),
                platform=incoming.platform,
                external_comment_id=incoming.external_comment_id,
                author_platform_user_id=incoming.author_platform_user_id,
                text=incoming.text,
                correlation_id=comment.correlation_id,
                status=CommentStatus.DEAD_LETTERED,
                created_at=now,
            )
            self._tables.comments[row.id] = row
            return TransitionOutcome.APPLIED
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
    async def add(
        self, seller_id: UUID, session: NewLiveSession, *, now: datetime
    ) -> LiveSessionRecord:
        _require_aware(now)
        # fk_live_sessions_seller_id_sellers, then the platform's unique live id.
        _row(self._tables.sellers, seller_id, "seller")
        self._check_live_id_free(session.platform, session.external_live_id, keep=None)
        record = LiveSessionRecord(
            id=self._db.new_id(),
            seller_id=seller_id,
            title=session.title,
            platform=session.platform,
            external_live_id=session.external_live_id,
            status=LiveSessionStatus.DRAFT,
            created_at=now,
        )
        self._tables.sessions[record.id] = record
        return record

    async def get(self, session_id: UUID) -> LiveSessionRecord | None:
        return self._tables.sessions.get(session_id)

    async def get_for_seller(self, seller_id: UUID, session_id: UUID) -> LiveSessionRecord | None:
        return self._db.session_of(seller_id, session_id)

    async def list_for_seller(
        self, seller_id: UUID, *, limit: int, cursor: UUID | None = None
    ) -> Page[LiveSessionRecord]:
        rows = sorted(
            (
                session
                for session in self._tables.sessions.values()
                if session.seller_id == seller_id and (cursor is None or session.id < cursor)
            ),
            key=lambda session: session.id,
            reverse=True,
        )
        return page_of(rows[: limit + 1], limit)

    async def update(
        self, seller_id: UUID, session_id: UUID, changes: LiveSessionChanges
    ) -> LiveSessionRecord:
        session = self._db.session_of(seller_id, session_id)
        if session is None:
            raise RecordNotFound(f"no live session {session_id} for seller {seller_id}")
        check_session_edit(session.status, keys=changes.touches_keys(session))
        updated = replace(
            session,
            title=changes.title if changes.title is not None else session.title,
            platform=changes.platform if changes.platform is not None else session.platform,
            external_live_id=(
                changes.external_live_id
                if changes.external_live_id is not None
                else session.external_live_id
            ),
        )
        self._check_live_id_free(updated.platform, updated.external_live_id, keep=session.id)
        self._tables.sessions[session_id] = updated
        return updated

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
        self, seller_id: UUID, session_id: UUID, target: LiveSessionStatus, *, now: datetime
    ) -> LiveSessionTransition:
        _require_aware(now)
        session = self._db.session_of(seller_id, session_id)
        if session is None:
            raise RecordNotFound(f"no live session {session_id} for seller {seller_id}")
        outcome = transition(LIVE_SESSION_TRANSITIONS, session.status, target)
        if outcome is TransitionOutcome.APPLIED:
            # Only draft -> live and live -> ended exist.
            if target is LiveSessionStatus.LIVE:
                session = replace(session, status=target, started_at=now)
            else:
                session = replace(session, status=target, ended_at=now)
            self._tables.sessions[session_id] = session
        return LiveSessionTransition(session, outcome)

    async def find_current(self, seller_id: UUID) -> LiveSessionRecord | None:
        live = [
            session
            for session in self._tables.sessions.values()
            if session.seller_id == seller_id and session.status is LiveSessionStatus.LIVE
        ]
        # The latest start first, a session without one last, then the newest id: as in SQL.
        return max(
            live,
            key=lambda session: (
                session.started_at is not None,
                session.started_at or _EARLIEST,
                session.id,
            ),
            default=None,
        )

    def _check_live_id_free(
        self, platform: Platform, external_live_id: str, *, keep: UUID | None
    ) -> None:
        """uq_live_sessions_platform_external_live_id, whatever the seller of the other session."""
        if any(
            session.id != keep
            and session.platform is platform
            and session.external_live_id == external_live_id
            for session in self._tables.sessions.values()
        ):
            raise LiveSessionConflict(f"another session has {platform}/{external_live_id}")


class InMemoryProductStockRepository(_Repository):
    async def add(
        self, seller_id: UUID, session_id: UUID, product: NewProduct, *, now: datetime
    ) -> ProductRecord:
        _require_aware(now)
        session = self._db.session_of(seller_id, session_id)
        if session is None:
            raise RecordNotFound(f"no live session {session_id} for seller {seller_id}")
        check_session_edit(session.status)
        catalog = self._rows_of(session_id)
        self._check_fits(catalog, product.code, product.variant)
        if len(catalog) >= MAX_PRODUCTS_PER_SESSION:
            raise ProductConflict(CatalogConflict.FULL)
        record = ProductRecord(
            id=self._db.new_id(),
            session_id=session_id,
            code=product.code,
            variant=product.variant,
            name=product.name,
            price=Money(product.price_minor, self._db.currency),
            stock=StockLevel(product.stock_total),
        )
        self._tables.products[record.id] = record
        return record

    async def get(self, product_id: UUID) -> ProductRecord | None:
        return self._tables.products.get(product_id)

    async def get_for_seller(
        self, seller_id: UUID, session_id: UUID, product_id: UUID
    ) -> ProductRecord | None:
        product = self._tables.products.get(product_id)
        if product is None or product.session_id != session_id:
            return None
        return product if self._db.session_of(seller_id, session_id) is not None else None

    async def list_for_session(self, session_id: UUID) -> list[ProductRecord]:
        return self._rows_of(session_id)

    async def list_for_seller(
        self, seller_id: UUID, session_id: UUID, *, limit: int, cursor: UUID | None = None
    ) -> Page[ProductRecord]:
        if self._db.session_of(seller_id, session_id) is None:
            raise RecordNotFound(f"no live session {session_id} for seller {seller_id}")
        rows = [row for row in self._rows_of(session_id) if cursor is None or row.id > cursor]
        return page_of(rows[: limit + 1], limit)

    async def update(
        self, seller_id: UUID, session_id: UUID, product_id: UUID, changes: ProductChanges
    ) -> ProductRecord:
        session = self._db.session_of(seller_id, session_id)
        product = self._tables.products.get(product_id)
        if session is None or product is None or product.session_id != session_id:
            raise RecordNotFound(f"no product {product_id} in session {session_id}")
        keys = changes.touches_keys(product)
        check_session_edit(session.status, keys=keys)
        code, variant = changes.key_of(product)
        if keys:
            others = [row for row in self._rows_of(session_id) if row.id != product_id]
            self._check_fits(others, code, variant)
        updated = replace(
            product,
            code=code,
            variant=variant,
            name=changes.name if changes.name is not None else product.name,
            price=(
                Money(changes.price_minor, product.price.currency)
                if changes.price_minor is not None
                else product.price
            ),
        )
        self._tables.products[product_id] = updated
        return updated

    def _rows_of(self, session_id: UUID) -> list[ProductRecord]:
        """The session's products, in creation order."""
        return [row for row in self._tables.products.values() if row.session_id == session_id]

    @staticmethod
    def _check_fits(catalog: Sequence[ProductRecord], code: ProductCode, variant: Variant) -> None:
        same_code = [row.variant for row in catalog if row.code == code]
        conflict = variant_conflict(same_code, variant)
        if conflict is not None:
            raise ProductConflict(conflict)

    async def restock(
        self, seller_id: UUID, session_id: UUID, product_id: UUID, delta: int
    ) -> ProductRecord:
        session = self._db.session_of(seller_id, session_id)
        product = self._tables.products.get(product_id)
        if session is None or product is None or product.session_id != session_id:
            raise RecordNotFound(f"no product {product_id} in session {session_id}")
        check_session_edit(session.status)
        stock = product.stock
        total = stock.total + delta
        if total < stock.reserved + stock.sold:
            raise StockConflict(
                f"product {product_id}: a total of {total} is below the "
                f"{stock.reserved} reserved and {stock.sold} sold"
            )
        if total > MAX_STOCK_TOTAL:
            raise StockConflict(
                f"product {product_id}: a total of {total} is above {MAX_STOCK_TOTAL}",
                above_maximum=True,
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
            self._tables.order_token_hashes[order.id] = self._db.access_token_hash(order.id)
            self._tables.products[product.id] = replace(product, stock=stock)
        self._tables.comments[comment.id] = replace(
            comment,
            status=CommentStatus.PROCESSED,
            session_id=session.id,
            parse=request.parse if request.parse is not None else comment.parse,
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

    async def find_by_reference(self, reference: OrderReference) -> BuyerOrderRecord | None:
        order = next(
            (row for row in self._tables.orders.values() if row.reference == reference), None
        )
        if order is None:
            return None
        product = self._tables.products[order.product_id]
        return BuyerOrderRecord(
            order=order,
            product_code=product.code,
            product_variant=product.variant,
            product_name=product.name,
            access_token_hash=self._tables.order_token_hashes[order.id],
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

    async def expire_due(self, *, now: datetime, limit: int) -> ExpiredReservations:
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
        released = sorted({order.product_id for order in expired})
        return ExpiredReservations(
            tuple(expired),
            {product_id: self._tables.products[product_id].stock for product_id in released},
        )

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


class InMemoryLiveSessionActivity(_Repository):
    """The snapshot of a session over the in-memory tables: ids are sequential, so newest first."""

    async def snapshot(self, session_id: UUID, *, limit: int) -> SessionActivity:
        tables = self._tables
        session = _row(tables.sessions, session_id, "live session")
        orders_by_intent = {order.intent_id: order.id for order in tables.orders.values()}
        intents = [intent for intent in tables.intents.values() if intent.session_id == session_id]
        rejections = [
            RejectedComment(comment.id, comment.rejection_reason)
            for comment in tables.comments.values()
            if comment.session_id == session_id and comment.rejection_reason is not None
        ]
        orders = [order for order in tables.orders.values() if order.session_id == session_id]
        return SessionActivity(
            session=session,
            products=tuple(
                product for product in tables.products.values() if product.session_id == session_id
            ),
            intents=tuple(
                RecordedIntent(intent, orders_by_intent.get(intent.id))
                for intent in _newest(intents, limit, key=lambda intent: intent.id)
            ),
            rejections=tuple(_newest(rejections, limit, key=lambda item: item.comment_id)),
            orders=tuple(_newest(orders, limit, key=lambda order: order.id)),
        )


def _newest[R](rows: Sequence[R], limit: int, *, key: Callable[[R], UUID]) -> list[R]:
    """The ``limit`` rows of highest id, highest first: the newest, as UUID v7 ids sort."""
    return sorted(rows, key=key, reverse=True)[:limit]


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
