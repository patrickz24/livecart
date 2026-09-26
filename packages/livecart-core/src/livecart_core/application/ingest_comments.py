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

The API verifies the signature, applies the rate limit and validates the
batch before it calls this use case.
"""

from collections.abc import Callable, Sequence
from uuid import uuid4

from livecart_core.application.dto import IncomingComment, QueuedComment
from livecart_core.application.ports import Clock, CommentQueue


def new_correlation_id() -> str:
    """A random UUID: unique across API instances without any coordination."""
    return str(uuid4())


class IngestComments:
    """Queue a verified batch; ``ServiceUnavailable`` when the queue did not take it all.

    ``comments_received_total`` counts the comments queued, named after the
    Prometheus counter that will expose it once the API has its metrics port.
    """

    def __init__(
        self,
        queue: CommentQueue,
        clock: Clock,
        *,
        correlation_ids: Callable[[], str] = new_correlation_id,
    ) -> None:
        self._queue = queue
        self._clock = clock
        self._correlation_ids = correlation_ids
        self.comments_received_total = 0

    def envelopes(
        self, comments: Sequence[IncomingComment], *, request_id: str
    ) -> list[QueuedComment]:
        """The envelope of each comment, in order."""
        now = self._clock.now()
        return [
            QueuedComment(
                comment=comment,
                correlation_id=self._correlation_ids(),
                request_id=request_id,
                enqueued_at=now,
            )
            for comment in comments
        ]

    async def __call__(self, comments: Sequence[IncomingComment], *, request_id: str) -> int:
        """Queue the comments in order and return how many were queued."""
        envelopes = self.envelopes(comments, request_id=request_id)
        await self._queue.enqueue(envelopes)
        self.comments_received_total += len(envelopes)
        return len(envelopes)
