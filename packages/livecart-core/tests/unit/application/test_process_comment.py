import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    MAX_IDENTIFIER_LENGTH,
    BuyerNotification,
    FallbackReceipt,
    InboxComment,
    IntentSource,
    LiveSessionRecord,
    OrderRecord,
    ParseKind,
    ParseSummary,
    Platform,
    QueuedComment,
    QueueDelivery,
    ReservationRequest,
    ReservationResult,
    SessionEventType,
    StockLevel,
)
from livecart_core.application.ports import NullIntentFallback, RecordNotFound, ServiceUnavailable
from livecart_core.application.process_comment import (
    HandledComment,
    Handling,
    ProcessComment,
    lease_holder,
)
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.outcomes import (
    AmbiguityReason,
    IntentOutcome,
    LlmOutcome,
    RejectionReason,
)
from livecart_core.domain.state_machines import (
    CommentStatus,
    LiveSessionStatus,
    OrderStatus,
    TransitionOutcome,
)
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, demo_catalog, queued_comment
from livecart_core.testing.fakes import (
    TEST_ORDER_TOKENS,
    FakeBuyerNotifier,
    FakeClock,
    FakeCommentQueue,
    FakeEventPublisher,
    InMemoryCommentInbox,
    InMemoryDatabase,
    InMemoryLiveSessionRepository,
    InMemoryOrderRepository,
)

LEASE = timedelta(seconds=30)
DB_BUDGET = timedelta(seconds=5)
TTL = timedelta(seconds=900)
CONSUMER = "worker-1"
BUY_B07 = queued_comment("je prends B07", external_comment_id="comment-1", author="buyer-1")


@dataclass
class World:
    database: InMemoryDatabase
    clock: FakeClock
    queue: FakeCommentQueue
    events: FakeEventPublisher
    notifier: FakeBuyerNotifier
    session: LiveSessionRecord
    process: ProcessComment

    async def deliver(self, *comments: QueuedComment) -> list[QueueDelivery]:
        await self.queue.enqueue(comments)
        return await self.queue.receive(CONSUMER, max_messages=100, block_seconds=0)

    async def handle(self, comment: QueuedComment = BUY_B07) -> HandledComment:
        [delivery] = await self.deliver(comment)
        return await self.process(delivery, consumer=CONSUMER)

    def row(self, comment_id: UUID) -> InboxComment:
        return self.database.tables.comments[comment_id]

    def event_types(self) -> list[SessionEventType]:
        return [event.type for event in self.events.published]


def _world(
    *,
    stock_total: int = 10,
    status: LiveSessionStatus = LiveSessionStatus.LIVE,
    inbox: Callable[[InMemoryDatabase], InMemoryCommentInbox] = InMemoryCommentInbox,
    sessions: Callable[[InMemoryDatabase], InMemoryLiveSessionRepository] = (
        InMemoryLiveSessionRepository
    ),
    orders: Callable[[InMemoryDatabase], InMemoryOrderRepository] = InMemoryOrderRepository,
) -> World:
    """A session selling the demo catalog, and ProcessComment over the fakes (or wrappers)."""
    database = InMemoryDatabase()
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    events = FakeEventPublisher()
    notifier = FakeBuyerNotifier()
    session = database.add_live_session(status=status)
    database.add_catalog(session.id, demo_catalog(), stock_total=stock_total)
    process = ProcessComment(
        inbox=inbox(database),
        sessions=sessions(database),
        orders=orders(database),
        queue=queue,
        events=events,
        notifier=notifier,
        access_token=TEST_ORDER_TOKENS.token_for,
        fallback=NullIntentFallback(),
        clock=clock,
        lease=LEASE,
        db_budget=DB_BUDGET,
        reservation_ttl=TTL,
        max_quantity=DEMO_MAX_QUANTITY,
    )
    return World(database, clock, queue, events, notifier, session, process)


# --- A comment that reserves -----------------------------------------------------------


async def test_a_match_reserves_opens_the_order_publishes_then_acknowledges() -> None:
    world = _world()
    world.clock.advance(5)

    handled = await world.handle()

    assert handled.handling is Handling.PROCESSED
    reservation = handled.reservation
    assert reservation is not None
    assert reservation.intent.outcome is IntentOutcome.RESERVED
    assert reservation.intent.source is IntentSource.PARSER
    assert reservation.stock == StockLevel(10, reserved=1)
    order = reservation.order
    assert order is not None
    assert order.status is OrderStatus.PENDING_PAYMENT
    assert order.buyer_platform_user_id == "buyer-1"
    assert order.reserved_until == world.clock.now() + TTL
    row = world.row(handled.comment.id)
    assert row.status is CommentStatus.PROCESSED
    assert row.session_id == world.session.id
    assert row.parse == ParseSummary(ParseKind.MATCH)
    assert world.event_types() == [
        SessionEventType.INTENT_RECORDED,
        SessionEventType.ORDER_UPDATED,
        SessionEventType.STOCK_UPDATED,
    ]
    assert {event.correlation_id for event in world.events.published} == {BUY_B07.correlation_id}
    assert {event.session_id for event in world.events.published} == {world.session.id}
    # Identifiers and counters only: never the buyer's platform identity.
    assert "buyer-1" not in repr([event.data for event in world.events.published])
    assert world.queue.pending == []
    assert len(world.queue.acknowledged) == 1


async def test_the_lease_belongs_to_the_delivery_until_the_message_deadline() -> None:
    world = _world()
    [delivery] = await world.deliver(BUY_B07)

    handled = await world.process(delivery, consumer=CONSUMER)

    assert handled.comment.claimed_by == f"{CONSUMER}/{delivery.delivery_id}"
    assert handled.comment.claimed_until == world.clock.now() + LEASE


async def test_short_stock_records_an_out_of_stock_intent_without_order() -> None:
    world = _world(stock_total=0)

    handled = await world.handle()

    assert handled.handling is Handling.PROCESSED
    assert handled.reservation is not None
    assert handled.reservation.intent.outcome is IntentOutcome.OUT_OF_STOCK
    assert handled.reservation.order is None
    assert world.event_types() == [
        SessionEventType.INTENT_RECORDED,
        SessionEventType.STOCK_UPDATED,
    ]
    assert world.events.published[0].data["order_id"] is None


# --- Comments that reserve nothing -----------------------------------------------------


async def test_a_comment_of_no_known_session_is_rejected_without_an_event() -> None:
    world = _world()
    stray = queued_comment("je prends B07", external_live_id="live-unknown")

    handled = await world.handle(stray)

    assert handled.handling is Handling.REJECTED
    assert handled.rejection is RejectionReason.SESSION_NOT_FOUND
    row = world.row(handled.comment.id)
    assert row.status is CommentStatus.REJECTED
    assert (row.session_id, row.parse) == (None, None)
    assert world.events.published == []
    assert world.queue.pending == []


@pytest.mark.parametrize("status", [LiveSessionStatus.DRAFT, LiveSessionStatus.ENDED])
async def test_a_comment_of_a_session_not_live_is_rejected(status: LiveSessionStatus) -> None:
    world = _world(status=status)

    handled = await world.handle()

    assert handled.rejection is RejectionReason.SESSION_NOT_LIVE
    row = world.row(handled.comment.id)
    assert (row.session_id, row.parse) == (world.session.id, None)
    [event] = world.events.published
    assert event.type is SessionEventType.COMMENT_REJECTED
    assert event.data == {"comment_id": str(row.id), "reason": "session_not_live"}
    assert world.queue.pending == []


async def test_a_parser_rejection_settles_the_comment_with_its_reason() -> None:
    world = _world()

    handled = await world.handle(queued_comment("trop beau le C3"))

    assert handled.handling is Handling.REJECTED
    assert handled.rejection is RejectionReason.NO_INTENT
    row = world.row(handled.comment.id)
    assert row.parse == ParseSummary(ParseKind.REJECTED, RejectionReason.NO_INTENT)
    assert world.event_types() == [SessionEventType.COMMENT_REJECTED]
    assert world.database.tables.intents == {}


async def test_an_ambiguity_stays_unresolved_without_an_llm() -> None:
    world = _world()

    handled = await world.handle(queued_comment("je prends A13"))

    assert handled.rejection is RejectionReason.AMBIGUOUS_UNRESOLVED
    assert handled.llm_outcome is LlmOutcome.DISABLED
    row = world.row(handled.comment.id)
    assert row.parse == ParseSummary(ParseKind.AMBIGUOUS, AmbiguityReason.UNKNOWN_CODE)
    assert row.llm_receipt == FallbackReceipt(LlmOutcome.DISABLED)
    assert world.event_types() == [SessionEventType.COMMENT_REJECTED]


async def test_the_parser_reads_the_text_of_the_first_delivery() -> None:
    world = _world()
    await world.database.comments.claim(
        BUY_B07, holder="crashed/1-0", now=world.clock.now(), lease=LEASE
    )
    world.clock.advance(LEASE + timedelta(seconds=1))
    edited = queued_comment("trop beau", external_comment_id="comment-1", author="buyer-1")

    handled = await world.handle(edited)

    assert handled.handling is Handling.PROCESSED
    assert handled.reservation is not None
    assert handled.reservation.intent.evidence == ("prends", "B07")


# --- Several deliveries of one comment -------------------------------------------------


async def test_a_redelivered_processed_comment_republishes_its_outcome() -> None:
    world = _world()
    first, again = await world.deliver(BUY_B07, BUY_B07)
    processed = await world.process(first, consumer=CONSUMER)
    world.events.published.clear()

    duplicate = await world.process(again, consumer="worker-2")

    assert duplicate.handling is Handling.DUPLICATE
    assert duplicate.reservation is not None
    assert processed.reservation is not None
    assert duplicate.reservation.intent == processed.reservation.intent
    assert duplicate.reservation.order is not None
    assert processed.reservation.order is not None
    assert duplicate.reservation.order.id == processed.reservation.order.id
    # Stamped by the first delivery: the second sends no notice.
    assert duplicate.reservation.order.buyer_notified_at == world.clock.now()
    assert not duplicate.notified
    assert len(world.notifier.sent) == 1
    assert world.event_types() == [
        SessionEventType.INTENT_RECORDED,
        SessionEventType.ORDER_UPDATED,
        SessionEventType.STOCK_UPDATED,
    ]
    assert len(world.database.tables.orders) == 1
    assert world.queue.acknowledged == [first.delivery_id, again.delivery_id]


async def test_a_redelivered_rejection_republishes_the_rejection() -> None:
    world = _world()
    first, again = await world.deliver(*[queued_comment("trop beau le C3")] * 2)
    await world.process(first, consumer=CONSUMER)
    world.events.published.clear()

    duplicate = await world.process(again, consumer=CONSUMER)

    assert duplicate.handling is Handling.DUPLICATE
    assert duplicate.rejection is RejectionReason.NO_INTENT
    assert world.event_types() == [SessionEventType.COMMENT_REJECTED]
    assert world.queue.pending == []


async def test_a_redelivery_of_a_comment_without_session_or_outcome_publishes_nothing() -> None:
    world = _world()
    stray = queued_comment(external_comment_id="stray", external_live_id="live-unknown")
    first, again = await world.deliver(stray, stray)
    await world.process(first, consumer=CONSUMER)
    dead = queued_comment(external_comment_id="dead")
    [dead_delivery] = await world.deliver(dead)
    await world.database.comments.claim(dead, holder="w/1-0", now=world.clock.now(), lease=LEASE)
    await world.database.comments.dead_letter(dead, now=world.clock.now())

    stray_again = await world.process(again, consumer=CONSUMER)
    dead_again = await world.process(dead_delivery, consumer=CONSUMER)

    assert (stray_again.handling, dead_again.handling) == (Handling.DUPLICATE,) * 2
    assert (stray_again.rejection, dead_again.rejection) == (
        RejectionReason.SESSION_NOT_FOUND,
        None,
    )
    assert world.events.published == []
    assert world.queue.pending == []


async def test_a_processed_comment_without_intent_republishes_nothing() -> None:
    world = _world()
    first, again = await world.deliver(BUY_B07, BUY_B07)
    handled = await world.process(first, consumer=CONSUMER)
    world.database.tables.intents.clear()
    world.events.published.clear()

    duplicate = await world.process(again, consumer=CONSUMER)

    assert duplicate.handling is Handling.DUPLICATE
    assert duplicate.reservation is None
    assert duplicate.comment.id == handled.comment.id
    assert world.events.published == []


async def test_a_delivery_finding_the_lease_held_is_acknowledged_only() -> None:
    world = _world()
    [delivery] = await world.deliver(BUY_B07)
    await world.database.comments.claim(
        BUY_B07, holder="worker-2/9-0", now=world.clock.now(), lease=LEASE
    )

    handled = await world.process(delivery, consumer=CONSUMER)

    assert handled.handling is Handling.IN_FLIGHT
    assert world.row(handled.comment.id).status is CommentStatus.RECEIVED
    assert world.events.published == []
    assert world.queue.acknowledged == [delivery.delivery_id]


async def test_a_delivery_takes_over_a_lease_that_ended() -> None:
    world = _world()
    await world.database.comments.claim(
        BUY_B07, holder="crashed/1-0", now=world.clock.now(), lease=LEASE
    )
    world.clock.advance(LEASE + timedelta(seconds=1))

    handled = await world.handle()

    assert handled.handling is Handling.PROCESSED


async def test_twenty_parallel_deliveries_of_one_comment_give_one_effect() -> None:
    world = _world()
    deliveries = await world.deliver(*[BUY_B07] * 20)

    handled = await asyncio.gather(
        *(world.process(delivery, consumer=f"worker-{n}") for n, delivery in enumerate(deliveries))
    )

    handlings = [result.handling for result in handled]
    assert handlings.count(Handling.PROCESSED) == 1
    assert set(handlings) <= {Handling.PROCESSED, Handling.DUPLICATE, Handling.IN_FLIGHT}
    assert len(world.database.tables.intents) == 1
    assert len(world.database.tables.orders) == 1
    assert _b07_stock(world) == StockLevel(10, reserved=1)
    assert world.queue.pending == []
    assert len(world.queue.acknowledged) == 20


def _b07_stock(world: World) -> StockLevel:
    product = world.database.find_product(world.session.id, ProductCode("B07"), Variant())
    assert product is not None
    return product.stock


# --- The buyer's notice ----------------------------------------------------------------


def _order(world: World) -> OrderRecord:
    [order] = world.database.tables.orders.values()
    return order


async def test_the_buyer_gets_the_reference_and_its_derived_token_then_the_notice_is_stamped() -> (
    None
):
    world = _world()
    world.clock.advance(7)

    handled = await world.handle()

    order = _order(world)
    assert handled.notified
    assert world.notifier.sent == [
        BuyerNotification(
            order_id=order.id,
            platform=Platform.SIMULATOR,
            recipient="buyer-1",
            reference=order.reference,
            access_token=TEST_ORDER_TOKENS.token_for(order.id),
        )
    ]
    # The token is not stored: only its hash is, and the notice proves it.
    stored_hash = world.database.tables.order_token_hashes[order.id]
    assert TEST_ORDER_TOKENS.verify(world.notifier.sent[0].access_token, stored_hash)
    assert order.buyer_notified_at == world.clock.now()
    assert world.queue.pending == []


async def test_an_intent_without_an_order_notifies_nobody() -> None:
    world = _world(stock_total=0)

    handled = await world.handle()

    assert not handled.notified
    assert world.notifier.sent == []


async def test_a_failed_notice_leaves_the_order_unstamped_and_its_retry_sends_the_same_token() -> (
    None
):
    world = _world()
    world.notifier.fail_next(1)
    [delivery] = await world.deliver(BUY_B07)

    with pytest.raises(ServiceUnavailable):
        await world.process(delivery, consumer=CONSUMER)
    assert _order(world).buyer_notified_at is None
    assert world.queue.pending == [delivery]
    retried = await world.process(delivery, consumer=CONSUMER)

    assert (retried.handling, retried.notified) == (Handling.DUPLICATE, True)
    order = _order(world)
    assert [notice.access_token for notice in world.notifier.sent] == [
        TEST_ORDER_TOKENS.token_for(order.id)
    ]
    assert order.buyer_notified_at == world.clock.now()
    assert world.queue.pending == []


class _CrashBeforeTheStamp(InMemoryOrderRepository):
    """The worker dies once between the notice and its stamp."""

    crashes = 1

    async def mark_buyer_notified(self, order_id: UUID, *, now: datetime) -> OrderRecord:
        if self.crashes:
            self.crashes -= 1
            raise ServiceUnavailable("database unavailable: scripted")
        return await super().mark_buyer_notified(order_id, now=now)


async def test_a_redelivery_after_a_crash_before_the_stamp_sends_the_same_token_again() -> None:
    world = _world(orders=_CrashBeforeTheStamp)
    [delivery] = await world.deliver(BUY_B07)
    with pytest.raises(ServiceUnavailable):
        await world.process(delivery, consumer=CONSUMER)
    world.clock.advance(LEASE * 4)
    [redelivery] = await world.queue.reclaim(
        "worker-2", min_idle_seconds=LEASE.total_seconds(), max_messages=1
    )

    handled = await world.process(redelivery, consumer="worker-2")

    first, second = world.notifier.sent
    assert (handled.handling, handled.notified) == (Handling.DUPLICATE, True)
    # The same message twice, never two tokens for one order.
    assert first == second
    assert first.access_token == TEST_ORDER_TOKENS.token_for(_order(world).id)
    assert _order(world).buyer_notified_at == world.clock.now()
    assert world.queue.pending == []


class _CrashBeforeTheAcknowledgment(FakeCommentQueue):
    """The worker dies once after the stamp, before its XACK."""

    crashes = 1

    async def ack(self, delivery: QueueDelivery) -> None:
        if self.crashes:
            self.crashes -= 1
            raise RuntimeError("the worker died")
        await super().ack(delivery)


async def test_a_redelivery_after_the_stamp_sends_no_second_notice() -> None:
    world = _world()
    crashing = _CrashBeforeTheAcknowledgment(world.clock)
    world.queue = crashing
    world.process = ProcessComment(
        inbox=world.database.comments,
        sessions=world.database.live_sessions,
        orders=world.database.orders,
        queue=crashing,
        events=world.events,
        notifier=world.notifier,
        access_token=TEST_ORDER_TOKENS.token_for,
        fallback=NullIntentFallback(),
        clock=world.clock,
        lease=LEASE,
        db_budget=DB_BUDGET,
        reservation_ttl=TTL,
        max_quantity=DEMO_MAX_QUANTITY,
    )
    [delivery] = await world.deliver(BUY_B07)
    with pytest.raises(RuntimeError, match="died"):
        await world.process(delivery, consumer=CONSUMER)
    world.clock.advance(LEASE * 4)
    [redelivery] = await world.queue.reclaim(
        "worker-2", min_idle_seconds=LEASE.total_seconds(), max_messages=1
    )

    handled = await world.process(redelivery, consumer="worker-2")

    assert (handled.handling, handled.notified) == (Handling.DUPLICATE, False)
    assert len(world.notifier.sent) == 1
    assert world.queue.pending == []


async def test_an_order_expired_before_its_notice_is_not_notified_any_more() -> None:
    world = _world(orders=_CrashBeforeTheStamp)
    [delivery] = await world.deliver(BUY_B07)
    with pytest.raises(ServiceUnavailable):
        await world.process(delivery, consumer=CONSUMER)
    world.clock.advance(TTL + timedelta(seconds=1))
    await world.database.orders.expire_due(now=world.clock.now(), limit=10)
    [redelivery] = await world.queue.reclaim("worker-2", min_idle_seconds=0, max_messages=1)

    handled = await world.process(redelivery, consumer="worker-2")

    assert (handled.handling, handled.notified) == (Handling.DUPLICATE, False)
    assert len(world.notifier.sent) == 1
    assert _order(world).buyer_notified_at is None


# --- Failures and races ----------------------------------------------------------------


class _FlakyOrders(InMemoryOrderRepository):
    """Unit 3 fails once, as a lost connection would, then works."""

    failures = 1

    async def reserve(self, request: ReservationRequest) -> ReservationResult | None:
        if self.failures:
            self.failures -= 1
            raise ServiceUnavailable("database unavailable: scripted")
        return await super().reserve(request)


async def test_a_failed_unit_leaves_the_delivery_pending_and_its_retry_takes_the_lease_again() -> (
    None
):
    world = _world(orders=_FlakyOrders)
    [delivery] = await world.deliver(BUY_B07)

    with pytest.raises(ServiceUnavailable):
        await world.process(delivery, consumer=CONSUMER)
    assert world.queue.pending == [delivery]
    world.clock.advance(1)
    retried = await world.process(delivery, consumer=CONSUMER)

    assert retried.handling is Handling.PROCESSED
    assert retried.comment.claimed_until == world.clock.now() + LEASE
    assert world.queue.pending == []


class _OvertakenOrders(InMemoryOrderRepository):
    """Another delivery settles the comment just before unit 3 of this one runs."""

    async def reserve(self, request: ReservationRequest) -> ReservationResult | None:
        await self._db.comments.reject(
            request.comment_id, RejectionReason.NO_INTENT, session_id=request.session_id
        )
        return await super().reserve(request)


async def test_unit_3_finding_the_comment_settled_handles_a_duplicate() -> None:
    world = _world(orders=_OvertakenOrders)

    handled = await world.handle()

    assert handled.handling is Handling.DUPLICATE
    assert handled.rejection is RejectionReason.NO_INTENT
    assert world.database.tables.intents == {}
    assert world.queue.pending == []


class _OvertakenInbox(InMemoryCommentInbox):
    """Another delivery rejects the comment just before this one does."""

    async def reject(
        self,
        comment_id: UUID,
        reason: RejectionReason,
        *,
        session_id: UUID | None,
        parse: ParseSummary | None = None,
    ) -> TransitionOutcome:
        await self._settle_first(comment_id, session_id)
        return await super().reject(comment_id, reason, session_id=session_id, parse=parse)

    async def _settle_first(self, comment_id: UUID, session_id: UUID | None) -> None:
        await super().reject(comment_id, RejectionReason.NO_INTENT, session_id=session_id)


class _DeadLetteredFirst(_OvertakenInbox):
    """The comment was dead-lettered just before this delivery rejects it."""

    async def _settle_first(self, comment_id: UUID, session_id: UUID | None) -> None:
        row = self._tables.comments[comment_id]
        comment = queued_comment(row.text, external_comment_id=row.external_comment_id)
        await self.dead_letter(comment, now=row.created_at)


@pytest.mark.parametrize(
    ("inbox", "rejection"),
    [(_OvertakenInbox, RejectionReason.NO_INTENT), (_DeadLetteredFirst, None)],
)
async def test_a_rejection_finding_the_comment_settled_handles_a_duplicate(
    inbox: type[InMemoryCommentInbox], rejection: RejectionReason | None
) -> None:
    world = _world(inbox=inbox)

    handled = await world.handle(queued_comment("je prends A13"))

    assert handled.handling is Handling.DUPLICATE
    assert handled.rejection is rejection
    assert world.queue.pending == []


class _ForgetfulInbox(_OvertakenInbox):
    """An inbox that loses the row it just settled: a bug the use case must not hide."""

    async def get(self, comment_id: UUID) -> InboxComment | None:
        return None


async def test_a_settled_comment_that_cannot_be_read_back_is_not_found() -> None:
    world = _world(inbox=_ForgetfulInbox)
    [delivery] = await world.deliver(queued_comment("je prends A13"))

    with pytest.raises(RecordNotFound):
        await world.process(delivery, consumer=CONSUMER)
    assert world.queue.pending == [delivery]


class _UnreachableSessions(InMemoryLiveSessionRepository):
    async def find_by_live_id(
        self, platform: Platform, external_live_id: str
    ) -> LiveSessionRecord | None:
        raise ServiceUnavailable("database unavailable: scripted")


async def test_an_unavailable_database_leaves_the_delivery_pending() -> None:
    world = _world(sessions=_UnreachableSessions)
    [delivery] = await world.deliver(BUY_B07)

    with pytest.raises(ServiceUnavailable):
        await world.process(delivery, consumer=CONSUMER)

    assert world.queue.pending == [delivery]
    assert world.queue.acknowledged == []


async def test_events_that_fail_to_publish_are_counted_never_raised() -> None:
    world = _world()
    world.events.fail_next(2)

    handled = await world.handle()
    rejected = await world.handle(queued_comment("trop beau", external_comment_id="c-2"))

    assert handled.handling is Handling.PROCESSED
    assert handled.unpublished_events == 2
    assert world.event_types() == [
        SessionEventType.STOCK_UPDATED,
        SessionEventType.COMMENT_REJECTED,
    ]
    assert rejected.unpublished_events == 0
    assert world.queue.pending == []


# --- Construction ----------------------------------------------------------------------


def test_the_lease_holder_is_the_consumer_and_the_delivery() -> None:
    assert lease_holder("worker-1", "1727352000000-3") == "worker-1/1727352000000-3"
    longest = "w" * (MAX_IDENTIFIER_LENGTH - len("/1-0"))
    assert len(lease_holder(longest, "1-0")) == MAX_IDENTIFIER_LENGTH
    with pytest.raises(ValueError, match="lease holder"):
        lease_holder(longest + "w", "1-0")
    with pytest.raises(ValueError, match="lease holder"):
        lease_holder("", "1-0")


@pytest.mark.parametrize(
    ("lease", "db_budget", "max_quantity", "message"),
    [
        (timedelta(0), DB_BUDGET, DEMO_MAX_QUANTITY, "lease"),
        (LEASE, timedelta(0), DEMO_MAX_QUANTITY, "db_budget"),
        (LEASE, DB_BUDGET, 0, "max_quantity"),
        (LEASE, DB_BUDGET, 100, "max_quantity"),
    ],
)
def test_nonsensical_settings_are_refused(
    lease: timedelta, db_budget: timedelta, max_quantity: int, message: str
) -> None:
    database = InMemoryDatabase()
    clock = FakeClock()
    with pytest.raises(ValueError, match=message):
        ProcessComment(
            inbox=database.comments,
            sessions=database.live_sessions,
            orders=database.orders,
            queue=FakeCommentQueue(clock),
            events=FakeEventPublisher(),
            notifier=FakeBuyerNotifier(),
            access_token=TEST_ORDER_TOKENS.token_for,
            fallback=NullIntentFallback(),
            clock=clock,
            lease=lease,
            db_budget=db_budget,
            reservation_ttl=TTL,
            max_quantity=max_quantity,
        )
