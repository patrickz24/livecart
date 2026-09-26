"""The worker's Prometheus metrics, in a registry of its own, served on the internal port.

``run`` serves ``WorkerMetrics.registry`` on ``METRICS_PORT`` (9100 in the
container; ``livecart_core.metrics``), never published on the host. The
loops record what they do:

- each handled comment (``record_handled``): its intent by source
  (``livecart_intents_detected_total{source}``, parser or llm), the order it
  opened (``livecart_orders_created_total``) or the reason it was rejected
  (``livecart_comments_rejected_total{reason}``, the closed list of
  ``RejectionReason``, each label present from the start so that a rate of
  zero is a zero, not a missing series), and the session events the
  publisher failed to append (``livecart_session_events_unpublished_total``);
- the time a comment spent queued, from its envelope's ``enqueued_at`` to
  the start of its handler (``livecart_queue_wait_seconds``), and the
  handler's own time, retries included (``livecart_comment_processing_seconds``);
- the deliveries that failed, by cause: deadline, transient (left for a
  reclaim) or error;
- each reclaim: the deliveries taken back from another consumer
  (``livecart_comments_reclaimed_total``), the dead letters
  (``livecart_comments_dead_lettered_total``), the entries trimmed, and the
  queue's backlog as the reclaim saw it (``livecart_queue_backlog``);
- each sweep tick: the reservations expired and their unpublished events.
- with the LLM fallback on (``watch_llm``), the plain counters of its
  ``LlmGuard``, read at scrape time: the receipts by outcome
  (``livecart_llm_outcomes_total{outcome}``, the closed list of
  ``LlmOutcome``), the dollars spent at list price
  (``livecart_llm_spend_usd_total``) and the costs the shared budget failed
  to record (``livecart_llm_spend_unrecorded_total``). Without a key there
  is no guard, and no LLM series: the ambiguities show as
  ``ambiguous_unresolved`` rejections.

Only identifiers of closed lists become labels, never a comment's, a
session's or a buyer's id, so the number of series stays fixed.
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import CounterMetricFamily, Metric
from prometheus_client.registry import Collector

from livecart_core.application.dto import IntentSource
from livecart_core.application.expire_reservations import SweepResult
from livecart_core.application.llm_guard import LlmGuard
from livecart_core.application.process_comment import HandledComment, Handling
from livecart_core.application.reclaim_comments import Reclaimed
from livecart_core.domain.outcomes import RejectionReason

# From 5 ms to 2 min: the queue is drained in milliseconds when the workers
# keep up, and a comment reclaimed after a crash waits about two minutes.
QUEUE_WAIT_BUCKETS: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)
# From 5 ms to the 30 s deadline: an LLM call (milestone 4c) takes seconds.
PROCESSING_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


class FailureCause(StrEnum):
    """Why a delivery was left unacknowledged: the label of ``livecart_comment_failures_total``."""

    DEADLINE = "deadline"
    TRANSIENT = "transient"
    ERROR = "error"


class LlmCollector(Collector):
    """The plain counters of the LLM guard, read at scrape time."""

    def __init__(self, guard: LlmGuard) -> None:
        self._guard = guard

    def collect(self) -> Iterable[Metric]:
        outcomes = CounterMetricFamily(
            "livecart_llm_outcomes_total",
            "Receipts of the LLM fallback, by outcome (a closed list).",
            labels=["outcome"],
        )
        for outcome, count in self._guard.outcomes.items():
            outcomes.add_metric([outcome.value], count)
        yield outcomes
        yield CounterMetricFamily(
            "livecart_llm_spend_usd_total",
            "Dollars the LLM calls cost at list price, as the budget counts them (approximate).",
            value=float(self._guard.spent_usd),
        )
        yield CounterMetricFamily(
            "livecart_llm_spend_unrecorded_total",
            "LLM call costs the shared budget failed to record (Redis unavailable).",
            value=self._guard.unrecorded_spend_total,
        )


class WorkerMetrics:
    """Every metric of one worker process, in ``registry``; see the module docstring."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        registry = self.registry
        self.intents_detected = Counter(
            "livecart_intents_detected_total",
            "Purchase intents recorded, by who resolved them.",
            ["source"],
            registry=registry,
        )
        self.orders_created = Counter(
            "livecart_orders_created_total",
            "Orders opened by a reservation.",
            registry=registry,
        )
        self.comments_rejected = Counter(
            "livecart_comments_rejected_total",
            "Comments settled without an intent, by reason (a closed list).",
            ["reason"],
            registry=registry,
        )
        self.comment_failures = Counter(
            "livecart_comment_failures_total",
            "Deliveries left unacknowledged, by cause: deadline, transient failure or error.",
            ["cause"],
            registry=registry,
        )
        self.queue_wait = Histogram(
            "livecart_queue_wait_seconds",
            "Time from a comment's queueing to the start of its handler.",
            buckets=QUEUE_WAIT_BUCKETS,
            registry=registry,
        )
        self.processing = Histogram(
            "livecart_comment_processing_seconds",
            "Time a comment's handler took, retries included.",
            buckets=PROCESSING_BUCKETS,
            registry=registry,
        )
        self.queue_backlog = Gauge(
            "livecart_queue_backlog",
            "The comment queue's backlog (lag + pending), as the last reclaim read it.",
            registry=registry,
        )
        self.comments_reclaimed = Counter(
            "livecart_comments_reclaimed_total",
            "Deliveries taken back from a consumer that left them idle.",
            registry=registry,
        )
        self.comments_dead_lettered = Counter(
            "livecart_comments_dead_lettered_total",
            "Comments moved to the dead-letter queue after their last delivery.",
            registry=registry,
        )
        self.queue_trimmed = Counter(
            "livecart_queue_trimmed_entries_total",
            "Settled entries trimmed from the comment stream.",
            registry=registry,
        )
        self.session_events_unpublished = Counter(
            "livecart_session_events_unpublished_total",
            "Session events the publisher failed to append after their commit (best effort).",
            registry=registry,
        )
        self.reservations_expired = Counter(
            "livecart_reservations_expired_total",
            "Unpaid reservations expired by the sweep, their stock released.",
            registry=registry,
        )
        # Every label of a closed list exists from the start, at zero.
        for source in IntentSource:
            self.intents_detected.labels(source=source.value)
        for reason in RejectionReason:
            self.comments_rejected.labels(reason=reason.value)
        for cause in FailureCause:
            self.comment_failures.labels(cause=cause.value)

    def watch_llm(self, guard: LlmGuard) -> None:
        """Serve the counters of the process's LLM guard with the other metrics."""
        self.registry.register(LlmCollector(guard))

    def record_handled(self, handled: HandledComment) -> None:
        """What one delivery settled; a duplicate or a comment in flight counts nothing again."""
        self.session_events_unpublished.inc(handled.unpublished_events)
        if handled.handling is Handling.PROCESSED and handled.reservation is not None:
            intent = handled.reservation.intent
            self.intents_detected.labels(source=intent.source.value).inc()
            if handled.reservation.order is not None:
                self.orders_created.inc()
        elif handled.handling is Handling.REJECTED and handled.rejection is not None:
            self.comments_rejected.labels(reason=handled.rejection.value).inc()

    def record_failure(self, cause: FailureCause) -> None:
        self.comment_failures.labels(cause=cause.value).inc()

    def record_reclaimed(self, reclaimed: Reclaimed) -> None:
        self.comments_reclaimed.inc(len(reclaimed.redelivered))
        self.comments_dead_lettered.inc(len(reclaimed.dead_letters))
        self.queue_trimmed.inc(reclaimed.trimmed)

    def record_sweep(self, result: SweepResult) -> None:
        self.reservations_expired.inc(result.expired)
        self.session_events_unpublished.inc(result.unpublished_events)
