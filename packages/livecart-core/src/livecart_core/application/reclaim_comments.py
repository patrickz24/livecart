"""``ReclaimComments``: what a crashed or stuck worker left pending, redelivered or dead-lettered.

Every worker runs it every 30 s, with the room its window has left:

1. ``CommentQueue.reclaim`` (``XAUTOCLAIM``) takes, for this consumer, the
   deliveries pending for at least the reclaim idle time, oldest first, as
   many as the window has room for; each one's delivery count goes up by one.
   The idle time is longer than a live worker ever holds a delivery (twice
   ``ceil(WORKER_BATCH / WORKER_CONCURRENCY)`` deadlines), so what comes back
   was left by a worker that crashed, or cut short at its deadline;
2. a delivery past ``max_deliveries`` is dead-lettered: the inbox first,
   which settles the comment as ``dead_lettered`` under the guard
   ``status = 'received'`` (a comment settled otherwise keeps its outcome),
   then the queue, which acknowledges the entry and copies it to the
   dead-letter stream. In that order, a crash between the two leaves the
   entry pending: the next reclaim does both again, the inbox's part as a
   ``NOOP``;
3. the other deliveries go back to the caller, to be processed like any
   delivery: the comment's lease says whether their first holder still
   works on them;
4. ``CommentQueue.trim`` drops the settled history, never an entry that is
   new or pending.

``ServiceUnavailable`` reaches the caller: the next tick tries again, and
whatever was reclaimed meanwhile stays pending in this consumer's list.
"""

from dataclasses import dataclass
from datetime import timedelta

from livecart_core.application.dto import QueueDelivery
from livecart_core.application.ports import Clock, CommentInbox, CommentQueue
from livecart_core.domain.errors import InvalidTransition
from livecart_core.domain.state_machines import TransitionOutcome


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """A delivery moved to the dead-letter queue, and what the inbox did with its comment.

    ``inbox`` is ``APPLIED`` when this dead letter settled the comment,
    ``NOOP`` when it was dead-lettered already, and ``None`` when another
    outcome had settled it: a processed comment whose acknowledgment kept
    failing, for instance.
    """

    delivery: QueueDelivery
    inbox: TransitionOutcome | None


@dataclass(frozen=True, slots=True)
class Reclaimed:
    """One tick: the deliveries to process again, the dead letters, the entries trimmed."""

    redelivered: tuple[QueueDelivery, ...] = ()
    dead_letters: tuple[DeadLetter, ...] = ()
    trimmed: int = 0


class ReclaimComments:
    """One tick of the reclaim loop; see the module docstring."""

    def __init__(
        self,
        queue: CommentQueue,
        inbox: CommentInbox,
        clock: Clock,
        *,
        min_idle: timedelta,
        max_deliveries: int,
    ) -> None:
        if min_idle <= timedelta(0) or max_deliveries < 1:
            raise ValueError("a reclaim needs an idle time and at least one delivery")
        self._queue = queue
        self._inbox = inbox
        self._clock = clock
        self._min_idle_seconds = min_idle.total_seconds()
        self._max_deliveries = max_deliveries

    async def __call__(self, consumer: str, *, room: int) -> Reclaimed:
        """Reclaim for ``consumer`` at most ``room`` deliveries, then trim."""
        if room < 0:
            raise ValueError(f"room cannot be negative, not {room}")
        reclaimed: list[QueueDelivery] = []
        if room:
            reclaimed = await self._queue.reclaim(
                consumer, min_idle_seconds=self._min_idle_seconds, max_messages=room
            )
        redelivered = []
        dead_letters = []
        for delivery in reclaimed:
            if delivery.delivery_count > self._max_deliveries:
                dead_letters.append(await self._dead_letter(delivery))
            else:
                redelivered.append(delivery)
        trimmed = await self._queue.trim()
        return Reclaimed(tuple(redelivered), tuple(dead_letters), trimmed)

    async def _dead_letter(self, delivery: QueueDelivery) -> DeadLetter:
        outcome: TransitionOutcome | None
        try:
            outcome = await self._inbox.dead_letter(delivery.comment, now=self._clock.now())
        except InvalidTransition:
            # Settled otherwise: the comment keeps its outcome, and the entry
            # still leaves the queue, where it would come back forever.
            outcome = None
        await self._queue.dead_letter(delivery)
        return DeadLetter(delivery, outcome)
