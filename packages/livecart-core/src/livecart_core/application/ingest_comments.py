"""``IngestComments``: the comments of a verified webhook, each in its envelope, onto the queue.

The webhook acknowledges a batch as soon as it is queued, and the workers do
the rest: nothing on this path reads or writes PostgreSQL, so a spike of
comments costs Redis appends only. Each comment gets its own envelope
(``QueuedComment``):

- a ``correlation_id`` of its own, created here, which every log line and
  session event the comment causes repeats, from the worker to the dashboard;
- the ``request_id`` of the webhook call that carried it (``X-Request-ID``),
  shared by the comments of one batch;
- ``enqueued_at``, from the ``Clock`` port, from which the worker measures
  the time spent in the queue.

Backpressure, before anything is queued:

- the backlog is what the workers have not acknowledged yet, delivered or not
  (``CommentQueue.backlog``, ``lag + pending`` on Redis), never the stream's
  length, which also counts a settled history not trimmed yet. Once it
  reaches ``max_depth``, the batch is refused with ``QueueFull`` (503 with
  ``Retry-After``) and the platform retries later;
- it is read at most once per ``backlog_ttl`` (1 s) per API instance, so a
  spike of webhooks does not become a spike of reads. Between two reads, the
  comments this instance queued are added to the value read, so its own
  batches count at once;
- an envelope larger than ``max_envelope_bytes`` as JSON is refused
  (``EnvelopeTooLarge``, 422): with the depth, that bound is what the startup
  check of the queue's memory relies on, so it holds by construction.

The API verifies the signature, applies the rate limit and validates the
batch before it calls this use case.
"""

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Final
from uuid import uuid4

from livecart_core.application.dto import IncomingComment, QueuedComment
from livecart_core.application.ports import Clock, CommentQueue

# How long a backlog read stays valid in one API instance.
BACKLOG_TTL: Final = timedelta(seconds=1)


def new_correlation_id() -> str:
    """A random UUID: unique across API instances without any coordination."""
    return str(uuid4())


class QueueFull(Exception):
    """The backlog reached the queue's depth: nothing was queued, the platform retries later."""

    def __init__(self, backlog: int, max_depth: int) -> None:
        super().__init__(f"the comment backlog is {backlog}, at or above {max_depth}")
        self.backlog = backlog
        self.max_depth = max_depth


class EnvelopeTooLarge(ValueError):
    """A comment whose envelope is over the queue's bound; ``index`` is its place in the batch."""

    def __init__(self, index: int, size: int, max_size: int) -> None:
        super().__init__(f"comment {index}: its envelope is {size} bytes, over {max_size}")
        self.index = index
        self.max_size = max_size


class IngestComments:
    """Queue a verified batch unless the backlog is full; see the module docstring.

    ``ServiceUnavailable`` from the queue, when it did not take the whole
    batch or could not tell its backlog, reaches the caller. The counters are
    named after the Prometheus metrics that will expose them once the API has
    its metrics port: ``livecart_comments_received_total``,
    ``livecart_queue_backpressure_total`` and ``livecart_queue_backlog``.
    """

    def __init__(
        self,
        queue: CommentQueue,
        clock: Clock,
        *,
        max_depth: int,
        max_envelope_bytes: int,
        backlog_ttl: timedelta = BACKLOG_TTL,
        correlation_ids: Callable[[], str] = new_correlation_id,
    ) -> None:
        if max_depth < 1 or max_envelope_bytes < 1:
            raise ValueError("max_depth and max_envelope_bytes must be positive")
        self._queue = queue
        self._clock = clock
        self._max_depth = max_depth
        self._max_envelope_bytes = max_envelope_bytes
        self._backlog_ttl = backlog_ttl
        self._correlation_ids = correlation_ids
        # One read at a time: the requests that find the value stale wait for it.
        self._backlog_lock = asyncio.Lock()
        self._backlog_read_at: datetime | None = None
        self._backlog_read = 0
        self._queued_since_read = 0
        self.comments_received_total = 0
        self.queue_backpressure_total = 0

    @property
    def queue_backlog(self) -> int:
        """The backlog as this instance last knew it: its last read, plus what it queued since."""
        return self._backlog_read + self._queued_since_read

    def envelopes(
        self, comments: Sequence[IncomingComment], *, request_id: str
    ) -> list[QueuedComment]:
        """The envelope of each comment, in order; ``EnvelopeTooLarge`` for an oversized one."""
        now = self._clock.now()
        envelopes = [
            QueuedComment(
                comment=comment,
                correlation_id=self._correlation_ids(),
                request_id=request_id,
                enqueued_at=now,
            )
            for comment in comments
        ]
        for index, envelope in enumerate(envelopes):
            size = len(envelope.model_dump_json().encode("utf-8"))
            if size > self._max_envelope_bytes:
                raise EnvelopeTooLarge(index, size, self._max_envelope_bytes)
        return envelopes

    async def __call__(self, comments: Sequence[IncomingComment], *, request_id: str) -> int:
        """Queue the comments in order and return how many were queued."""
        envelopes = self.envelopes(comments, request_id=request_id)
        backlog = await self._backlog()
        if backlog >= self._max_depth:
            self.queue_backpressure_total += 1
            raise QueueFull(backlog, self._max_depth)
        await self._queue.enqueue(envelopes)
        self._queued_since_read += len(envelopes)
        self.comments_received_total += len(envelopes)
        return len(envelopes)

    async def _backlog(self) -> int:
        if self._is_stale():
            async with self._backlog_lock:
                # Another request may have read it while this one waited.
                if self._is_stale():
                    self._backlog_read = await self._queue.backlog()
                    self._backlog_read_at = self._clock.now()
                    self._queued_since_read = 0
        return self.queue_backlog

    def _is_stale(self) -> bool:
        if self._backlog_read_at is None:
            return True
        age = self._clock.now() - self._backlog_read_at
        # A clock set back makes the age negative: read again rather than trust it.
        return not timedelta(0) <= age < self._backlog_ttl
