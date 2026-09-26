"""The comment consumer: the queue read in batches, each delivery handled within four bounds.

- **A window**: the consumer holds at most ``WORKER_BATCH`` (32) deliveries,
  read and not finished yet. It reads with ``XREADGROUP COUNT <room left>
  BLOCK 2000``, and waits for room when the window is full. So no delivery
  sits in its pending entries list for longer than ``ceil(batch /
  concurrency)`` deadlines while the worker lives, which is what the idle
  time before a reclaim (``QUEUE_RECLAIM_MIN_IDLE_MS``) is derived from; a
  worker reading faster than it handles would see its own deliveries
  reclaimed from under it.
- **A semaphore**: at most ``WORKER_CONCURRENCY`` (16) handlers run at once,
  each on a pooled connection of its own, so the handlers never wait for the
  pool (``WORKER_CONCURRENCY + 4`` connections, see the engine).
- **A deadline**: ``asyncio.timeout(MESSAGE_PROCESS_TIMEOUT_S)`` (30 s) per
  delivery, from when its handler starts, retries included. The comment's
  lease is as long and is taken once the handler runs, so no other delivery
  works on the comment while this one may.
- **Retries**: ``ServiceUnavailable``, a transient failure of the database
  (a lock or statement timeout, a lost connection) or of Redis, is retried
  twice in the process after a jittered delay: another attempt of the same
  delivery takes its lease again (``lease_holder``). After that, or at the
  deadline, the delivery is left pending: a reclaim hands it out again once
  idle, and dead-letters it after ``QUEUE_MAX_DELIVERIES``.

**The reclaim** (``ReclaimComments``) runs in the same loop, at startup and
then every 30 s, between two reads: ``XAUTOCLAIM`` of what other consumers
left idle, as many as the window has room for, the dead letters, then the
safe trim. The loop being the only one to fill the window, the room it
offers the reclaim is the window's own, and the reclaimed deliveries count in
it like the others. A reclaim loop of its own found the window taken
whenever it looked: the read loop spends its time in ``XREADGROUP BLOCK``,
with the room it asked for set aside, so a worker left idle never took back
what a crashed one had left.

A handler never raises into the loop: whatever it does is logged, with the
comment's correlation and request ids bound to every line. The token of an
order never reaches a log line (``HandledComment`` carries none).
"""

import asyncio
import contextlib
import os
import random
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

import structlog

from livecart_core.application.dto import QueueDelivery
from livecart_core.application.ports import CommentQueue, ServiceUnavailable
from livecart_core.application.process_comment import HandledComment
from livecart_core.application.reclaim_comments import Reclaimed

logger = structlog.get_logger(__name__)

# XREADGROUP BLOCK: how long a read waits for a first comment. The loop also
# looks at its stop event and marks its progress at least this often.
RECEIVE_BLOCK_S: Final = 2.0
# How often the loop reclaims what other consumers left idle, at startup first.
RECLAIM_INTERVAL_S: Final = 30.0
# Retries of a transient failure in the process, and the base of their
# exponential delays: each delay is drawn from 0 to base x 2 ** (retry - 1)
# ("full jitter"), so that handlers failing together do not retry together.
TRANSIENT_RETRIES: Final = 2
RETRY_BASE_DELAY_S: Final = 0.1
# A consumer's name in the group: its host and its process. The longest host
# names are cut, so that a lease holder (name and delivery id) fits its column.
MAX_HOST_NAME_LENGTH: Final = 64

_RANDOM: Final = random.SystemRandom()


def default_consumer_name() -> str:
    """``<host>-<pid>``: every replica, and every restart of one, is a consumer of its own."""
    return f"{socket.gethostname()[:MAX_HOST_NAME_LENGTH]}-{os.getpid()}"


def full_jitter(cap_s: float) -> float:
    return _RANDOM.uniform(0, cap_s)


class CommentHandler(Protocol):
    """What the consumer hands each delivery to: ``ProcessComment`` in the worker."""

    async def __call__(self, delivery: QueueDelivery, *, consumer: str) -> HandledComment: ...


class Reclaimer(Protocol):
    """One reclaim for a consumer, within the room it offers: ``ReclaimComments`` in the worker."""

    async def __call__(self, consumer: str, *, room: int) -> Reclaimed: ...


class CommentConsumer:
    """One consumer of the group ``workers``; see the module docstring.

    ``run`` reads, and reclaims when a ``reclaim`` is given, until its stop
    event is set, then waits for the handlers in flight.
    """

    def __init__(
        self,
        queue: CommentQueue,
        handler: CommentHandler,
        *,
        name: str,
        batch: int,
        concurrency: int,
        deadline_s: float,
        reclaim: Reclaimer | None = None,
        reclaim_interval_s: float = RECLAIM_INTERVAL_S,
        retries: int = TRANSIENT_RETRIES,
        retry_base_delay_s: float = RETRY_BASE_DELAY_S,
        block_s: float = RECEIVE_BLOCK_S,
        jitter: Callable[[float], float] = full_jitter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(batch, concurrency) < 1 or min(deadline_s, block_s, reclaim_interval_s) <= 0:
            raise ValueError("a consumer needs a window, handlers, a deadline and a block time")
        if retries < 0:
            raise ValueError(f"retries cannot be negative, not {retries}")
        self.name = name
        self._queue = queue
        self._handler = handler
        self._batch = batch
        self._slots = asyncio.Semaphore(concurrency)
        self._deadline_s = deadline_s
        self._reclaim = reclaim
        self._reclaim_interval_s = reclaim_interval_s
        self._retries = retries
        self._retry_base_delay_s = retry_base_delay_s
        self._block_s = block_s
        self._jitter = jitter
        self._sleep = sleep
        self._monotonic = monotonic
        # Counted by the handlers themselves, so that the room is back as soon
        # as one ends; the tasks are kept for drain, and against the garbage
        # collector, which may drop a task nothing references.
        self._in_flight = 0
        self._handlers: set[asyncio.Task[None]] = set()
        self._room_freed = asyncio.Event()
        self._last_progress = monotonic()

    @property
    def in_flight(self) -> int:
        """The deliveries this consumer holds: read or reclaimed, and not finished yet."""
        return self._in_flight

    @property
    def room(self) -> int:
        """How many more deliveries the window takes."""
        return self._batch - self._in_flight

    def progressed_within(self, seconds: float) -> bool:
        """Whether the read loop went round in the last ``seconds``: what the heartbeat checks."""
        return self._monotonic() - self._last_progress <= seconds

    def submit(self, delivery: QueueDelivery) -> None:
        """Hand one delivery to a handler, within the window."""
        if self.room < 1:
            raise RuntimeError(f"the window of {self._batch} deliveries is full")
        self._in_flight += 1
        handler = asyncio.create_task(
            self._handle(delivery), name=f"comment-{delivery.delivery_id}"
        )
        self._handlers.add(handler)
        handler.add_done_callback(self._handlers.discard)

    async def run(self, stop: asyncio.Event) -> None:
        """Read, and reclaim, until ``stop`` is set, then wait for the handlers."""
        logger.info("comment_consumer_started", consumer=self.name)
        next_reclaim = self._monotonic()
        try:
            while not stop.is_set():
                self._last_progress = self._monotonic()
                if self._reclaim is not None and self._last_progress >= next_reclaim:
                    await self.reclaim_once()
                    next_reclaim = self._monotonic() + self._reclaim_interval_s
                    continue
                if self.room < 1:
                    await self._wait_for_room()
                    continue
                try:
                    deliveries = await self._queue.receive(
                        self.name, max_messages=self.room, block_seconds=self._block_s
                    )
                except ServiceUnavailable as error:
                    logger.warning("comment_queue_unavailable", error=str(error))
                    await _pause(stop, self._block_s)
                    continue
                for delivery in deliveries:
                    self.submit(delivery)
        finally:
            await self.drain()
            logger.info("comment_consumer_stopped", consumer=self.name)

    async def reclaim_once(self) -> None:
        """One reclaim, within the room the window has left; a failure is logged, never raised."""
        if self._reclaim is None:
            raise RuntimeError("this consumer was given no reclaim")
        try:
            reclaimed = await self._reclaim(self.name, room=self.room)
        except ServiceUnavailable as error:
            logger.warning("queue_reclaim_unavailable", error=str(error))
            return
        except Exception:
            logger.exception("queue_reclaim_failed")
            return
        for delivery in reclaimed.redelivered:
            self.submit(delivery)
        for dead in reclaimed.dead_letters:
            envelope = dead.delivery.comment
            logger.error(
                "comment_dead_lettered",
                delivery_id=dead.delivery.delivery_id,
                delivery_count=dead.delivery.delivery_count,
                correlation_id=envelope.correlation_id,
                request_id=envelope.request_id,
                inbox=None if dead.inbox is None else dead.inbox.value,
            )
        if reclaimed.redelivered or reclaimed.dead_letters or reclaimed.trimmed:
            logger.info(
                "queue_reclaimed",
                redelivered=len(reclaimed.redelivered),
                dead_lettered=len(reclaimed.dead_letters),
                trimmed=reclaimed.trimmed,
            )

    async def drain(self) -> None:
        """Wait for every handler in flight; each ends by its deadline at the latest."""
        # Only the unfinished ones: a finished task leaves the set in a callback
        # that runs later, and waiting on it again would never yield.
        while running := {handler for handler in self._handlers if not handler.done()}:
            await asyncio.wait(running)

    async def _wait_for_room(self) -> None:
        """Until a handler finishes, or the block time passes so that the loop goes round."""
        self._room_freed.clear()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self._block_s):
                await self._room_freed.wait()

    async def _handle(self, delivery: QueueDelivery) -> None:
        try:
            await self._handle_logged(delivery)
        finally:
            self._in_flight -= 1
            self._room_freed.set()

    async def _handle_logged(self, delivery: QueueDelivery) -> None:
        envelope = delivery.comment
        with structlog.contextvars.bound_contextvars(
            correlation_id=envelope.correlation_id, request_id=envelope.request_id
        ):
            fields = {
                "delivery_id": delivery.delivery_id,
                "delivery_count": delivery.delivery_count,
            }
            async with self._slots:
                try:
                    async with asyncio.timeout(self._deadline_s):
                        handled = await self._attempts(delivery)
                except TimeoutError:
                    logger.warning(
                        "comment_deadline_exceeded", deadline_s=self._deadline_s, **fields
                    )
                except ServiceUnavailable as error:
                    logger.warning("comment_left_for_reclaim", error=str(error), **fields)
                except Exception:
                    logger.exception("comment_failed", **fields)
                else:
                    logger.info("comment_handled", **fields, **_summary(handled))

    async def _attempts(self, delivery: QueueDelivery) -> HandledComment:
        """The handler, and up to ``retries`` more attempts after a transient failure."""
        retry = 0
        while True:
            try:
                return await self._handler(delivery, consumer=self.name)
            except ServiceUnavailable as error:
                retry += 1
                if retry > self._retries:
                    raise
                delay_s = self._jitter(self._retry_base_delay_s * 2 ** (retry - 1))
                logger.info(
                    "comment_retrying",
                    retry=retry,
                    delay_s=round(delay_s, 3),
                    error=str(error),
                    delivery_id=delivery.delivery_id,
                )
                await self._sleep(delay_s)


def _summary(handled: HandledComment) -> dict[str, object]:
    """What a log line says of a handled comment: identifiers and outcomes only."""
    reservation = handled.reservation
    order = None if reservation is None else reservation.order
    return {
        "handling": handled.handling.value,
        "comment_id": str(handled.comment.id),
        "intent_outcome": None if reservation is None else reservation.intent.outcome.value,
        "order_id": None if order is None else str(order.id),
        "rejection_reason": None if handled.rejection is None else handled.rejection.value,
        "unpublished_events": handled.unpublished_events,
        "notified": handled.notified,
    }


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, or less if ``stop`` is set meanwhile."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await stop.wait()
