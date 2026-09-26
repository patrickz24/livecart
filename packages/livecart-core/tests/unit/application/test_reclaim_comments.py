from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from livecart_core.application.dto import QueuedComment, QueueDelivery
from livecart_core.application.ports import ServiceUnavailable
from livecart_core.application.reclaim_comments import DeadLetter, ReclaimComments, Reclaimed
from livecart_core.domain.outcomes import RejectionReason
from livecart_core.domain.state_machines import CommentStatus, TransitionOutcome
from livecart_core.testing.builders import queued_comment
from livecart_core.testing.fakes import (
    FakeClock,
    FakeCommentQueue,
    InMemoryCommentInbox,
    InMemoryDatabase,
)

IDLE = timedelta(seconds=120)
LEASE = timedelta(seconds=30)
MAX_DELIVERIES = 3


class CountingQueue(FakeCommentQueue):
    """The in-memory queue, counting its trims."""

    trims = 0

    async def trim(self) -> int:
        self.trims += 1
        return await super().trim()


@dataclass
class World:
    clock: FakeClock
    queue: CountingQueue
    database: InMemoryDatabase
    reclaim: ReclaimComments

    async def pending_for(self, *comments: QueuedComment, deliveries: int = 1) -> None:
        """The comments delivered ``deliveries`` times, never acknowledged, and idle since."""
        await self.queue.enqueue(comments)
        await self.queue.receive("crashed", max_messages=len(comments), block_seconds=0)
        for _ in range(deliveries - 1):
            await self.queue.reclaim("crashed", min_idle_seconds=0, max_messages=len(comments))
        self.clock.advance(IDLE)

    def status(self, comment: QueuedComment) -> CommentStatus | None:
        rows = self.database.tables.comments.values()
        incoming = comment.comment
        return next(
            (row.status for row in rows if row.external_comment_id == incoming.external_comment_id),
            None,
        )


class _Unreachable(InMemoryCommentInbox):
    async def dead_letter(self, comment: QueuedComment, *, now: datetime) -> TransitionOutcome:
        raise ServiceUnavailable("database unavailable: scripted")


def _world() -> World:
    clock = FakeClock()
    queue = CountingQueue(clock)
    database = InMemoryDatabase()
    reclaim = ReclaimComments(
        queue, database.comments, clock, min_idle=IDLE, max_deliveries=MAX_DELIVERIES
    )
    return World(clock, queue, database, reclaim)


def _ids(deliveries: tuple[QueueDelivery, ...]) -> list[str]:
    return [delivery.delivery_id for delivery in deliveries]


async def test_nothing_idle_long_enough_is_reclaimed_and_the_history_is_trimmed() -> None:
    world = _world()
    await world.queue.enqueue([queued_comment()])
    await world.queue.receive("worker-1", max_messages=1, block_seconds=0)
    world.clock.advance(IDLE - timedelta(seconds=1))

    reclaimed = await world.reclaim("worker-2", room=32)

    assert reclaimed == Reclaimed()
    assert world.queue.trims == 1
    assert len(world.queue.pending) == 1


async def test_idle_deliveries_are_handed_out_again_up_to_the_room_left() -> None:
    world = _world()
    await world.pending_for(*(queued_comment(external_comment_id=f"c-{n}") for n in range(3)))

    reclaimed = await world.reclaim("worker-2", room=2)

    assert _ids(reclaimed.redelivered) == ["1-0", "2-0"]
    assert [delivery.delivery_count for delivery in reclaimed.redelivered] == [2, 2]
    assert reclaimed.dead_letters == ()
    assert world.queue.dead_letters == []


async def test_a_full_window_reclaims_nothing_but_still_trims() -> None:
    world = _world()
    await world.pending_for(queued_comment())

    reclaimed = await world.reclaim("worker-2", room=0)

    assert reclaimed == Reclaimed()
    assert world.queue.trims == 1
    assert [delivery.delivery_count for delivery in world.queue.pending] == [1]


async def test_a_delivery_past_the_last_is_dead_lettered_in_the_inbox_then_the_queue() -> None:
    world = _world()
    comment = queued_comment(external_comment_id="c-1")
    await world.database.comments.claim(
        comment, holder="crashed/1-0", now=world.clock.now(), lease=LEASE
    )
    await world.pending_for(comment, deliveries=MAX_DELIVERIES)

    reclaimed = await world.reclaim("worker-2", room=32)

    [dead] = reclaimed.dead_letters
    assert dead.inbox is TransitionOutcome.APPLIED
    assert dead.delivery.delivery_count == MAX_DELIVERIES + 1
    assert reclaimed.redelivered == ()
    assert world.status(comment) is CommentStatus.DEAD_LETTERED
    assert world.queue.dead_letters == [dead.delivery]
    assert world.queue.pending == []


async def test_the_last_allowed_delivery_is_still_handed_out() -> None:
    world = _world()
    await world.pending_for(queued_comment(), deliveries=MAX_DELIVERIES - 1)

    reclaimed = await world.reclaim("worker-2", room=32)

    assert [delivery.delivery_count for delivery in reclaimed.redelivered] == [MAX_DELIVERIES]
    assert reclaimed.dead_letters == ()


async def test_a_comment_never_recorded_is_recorded_dead_lettered() -> None:
    world = _world()
    comment = queued_comment(external_comment_id="c-never")
    await world.pending_for(comment, deliveries=MAX_DELIVERIES)

    reclaimed = await world.reclaim("worker-2", room=32)

    assert [dead.inbox for dead in reclaimed.dead_letters] == [TransitionOutcome.APPLIED]
    assert world.status(comment) is CommentStatus.DEAD_LETTERED


async def test_a_comment_settled_otherwise_keeps_its_outcome_and_leaves_the_queue() -> None:
    world = _world()
    processed = queued_comment(external_comment_id="c-rejected")
    dead = queued_comment(external_comment_id="c-dead")
    claim = await world.database.comments.claim(
        processed, holder="crashed/1-0", now=world.clock.now(), lease=LEASE
    )
    await world.database.comments.reject(
        claim.comment.id, RejectionReason.NO_INTENT, session_id=None
    )
    await world.database.comments.dead_letter(dead, now=world.clock.now())
    await world.pending_for(processed, dead, deliveries=MAX_DELIVERIES)

    reclaimed = await world.reclaim("worker-2", room=32)

    assert [(dead.delivery.comment, dead.inbox) for dead in reclaimed.dead_letters] == [
        (processed, None),
        (dead, TransitionOutcome.NOOP),
    ]
    assert world.status(processed) is CommentStatus.REJECTED
    assert world.queue.pending == []
    assert len(world.queue.dead_letters) == 2


async def test_an_unavailable_inbox_leaves_the_delivery_pending_for_the_next_tick() -> None:
    world = _world()
    comment = queued_comment()
    await world.pending_for(comment, deliveries=MAX_DELIVERIES)

    failing = ReclaimComments(
        world.queue,
        _Unreachable(world.database),
        world.clock,
        min_idle=IDLE,
        max_deliveries=MAX_DELIVERIES,
    )
    with pytest.raises(ServiceUnavailable):
        await failing("worker-2", room=32)

    assert world.queue.dead_letters == []
    assert len(world.queue.pending) == 1
    world.clock.advance(IDLE)
    retried = await world.reclaim("worker-2", room=32)
    assert [dead.inbox for dead in retried.dead_letters] == [TransitionOutcome.APPLIED]


@pytest.mark.parametrize(("idle", "max_deliveries"), [(timedelta(0), 3), (IDLE, 0)])
def test_a_reclaim_needs_an_idle_time_and_a_delivery(idle: timedelta, max_deliveries: int) -> None:
    world = _world()

    with pytest.raises(ValueError, match="idle time"):
        ReclaimComments(
            world.queue,
            world.database.comments,
            world.clock,
            min_idle=idle,
            max_deliveries=max_deliveries,
        )


async def test_the_room_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        await _world().reclaim("worker-2", room=-1)


def test_a_dead_letter_names_its_delivery() -> None:
    delivery = QueueDelivery("1-0", queued_comment(), 4)

    assert DeadLetter(delivery, None).delivery is delivery
