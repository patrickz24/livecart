import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest

from livecart_core.application.dto import (
    MAX_IDENTIFIER_LENGTH,
    InboxComment,
    IntentSource,
    LiveSessionRecord,
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
from livecart_core.application.ports import RecordNotFound, ServiceUnavailable
from livecart_core.application.process_comment import (
    HandledComment,
    Handling,
    ProcessComment,
    lease_holder,
)
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.outcomes import AmbiguityReason, IntentOutcome, RejectionReason
from livecart_core.domain.state_machines import (
    CommentStatus,
    LiveSessionStatus,
    OrderStatus,
    TransitionOutcome,
)
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, demo_catalog, queued_comment
from livecart_core.testing.fakes import (
    FakeClock,
    FakeCommentQueue,
    FakeEventPublisher,
    InMemoryCommentInbox,
    InMemoryDatabase,
    InMemoryLiveSessionRepository,
    InMemoryOrderRepository,
)

LEASE = timedelta(seconds=30)
TTL = timedelta(seconds=900)
CONSUMER = "worker-1"
BUY_B07 = queued_comment("je prends B07", external_comment_id="comment-1", author="buyer-1")


@dataclass
class World:
    database: InMemoryDatabase
    clock: FakeClock
    queue: FakeCommentQueue
    events: FakeEventPublisher
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
    session = database.add_live_session(status=status)
    database.add_catalog(session.id, demo_catalog(), stock_total=stock_total)
    process = ProcessComment(
        inbox=inbox(database),
        sessions=sessions(database),
        orders=orders(database),
        queue=queue,
        events=events,
        clock=clock,
        lease=LEASE,
        reservation_ttl=TTL,
        max_quantity=DEMO_MAX_QUANTITY,
    )
    return World(database, clock, queue, events, session, process)


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


async def test_an_ambiguity_stays_unresolved_until_the_llm_fallback() -> None:
    world = _world()

    handled = await world.handle(queued_comment("je prends A13"))

    assert handled.rejection is RejectionReason.AMBIGUOUS_UNRESOLVED
    row = world.row(handled.comment.id)
    assert row.parse == ParseSummary(ParseKind.AMBIGUOUS, AmbiguityReason.UNKNOWN_CODE)
    assert row.llm_receipt is None
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
    assert duplicate.reservation == processed.reservation
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
    await world.database.comments.dead_letter(Platform.SIMULATOR, "dead")

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
        await self.dead_letter(row.platform, row.external_comment_id)


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
    ("lease", "max_quantity", "message"),
    [
        (timedelta(0), DEMO_MAX_QUANTITY, "lease"),
        (LEASE, 0, "max_quantity"),
        (LEASE, 100, "max_quantity"),
    ],
)
def test_nonsensical_settings_are_refused(
    lease: timedelta, max_quantity: int, message: str
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
            clock=clock,
            lease=lease,
            reservation_ttl=TTL,
            max_quantity=max_quantity,
        )
