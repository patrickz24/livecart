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
- each sweep tick: the reservations expired and their unpublished events;
  of the due orders with a payment in flight, each the sweep asked the
  provider about, by outcome (``livecart_checkout_expiries_total{outcome}``,
  the closed list of ``CheckoutExpiryOutcome``: ``failed`` is the one to
  alert on), those it released counted with the expired reservations and
  those the buyer paid first with the orders paid;
- each tick of the payment events loop: every event it claimed, by result
  (``livecart_payment_events_total{result}``, the closed list of
  ``PaymentEventResult``: ``amount_mismatch`` and ``attempts_exhausted`` are
  the failures to alert on), the orders paid
  (``livecart_orders_paid_total``) and those paid after the sweep released
  their stock (``livecart_orders_paid_after_expiry_total``, each a refund to
  make by hand);
- each shipment of a paid order created at the carrier, by outcome
  (``livecart_shipment_creations_total{outcome,via}``, the closed list of
  ``ShipmentCreationOutcome``: ``no_address`` and ``failed`` need a person)
  and by path, ``payment`` (the tick that paid it) or ``fallback`` (a crash
  or an outage recovered);
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

from livecart_core.application.apply_payment_events import PaymentEventResult, PaymentEventsTick
from livecart_core.application.create_shipment import CreatedShipment, ShipmentCreationOutcome
from livecart_core.application.dto import IntentSource
from livecart_core.application.expire_checkouts import CheckoutExpiryOutcome, CheckoutExpiryTick
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


class ShipmentPath(StrEnum):
    """Which path created a shipment: the label ``via`` of ``livecart_shipment_creations_total``."""

    # The tick that paid the order: the payment events loop or the expiry sweep.
    PAYMENT = "payment"
    # The shipments loop's fallback, after a crash or an outage.
    FALLBACK = "fallback"


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
        self.checkout_expiries = Counter(
            "livecart_checkout_expiries_total",
            "Due orders with a payment in flight the sweep asked the provider about, by outcome.",
            ["outcome"],
            registry=registry,
        )
        self.payment_events = Counter(
            "livecart_payment_events_total",
            "Payment events the registry loop claimed, by result (a closed list).",
            ["result"],
            registry=registry,
        )
        self.orders_paid = Counter(
            "livecart_orders_paid_total",
            "Orders paid by a payment event, their reserved units sold.",
            registry=registry,
        )
        self.orders_paid_after_expiry = Counter(
            "livecart_orders_paid_after_expiry_total",
            "Orders paid after the sweep released their stock: each one a refund to make by hand.",
            registry=registry,
        )
        self.shipment_creations = Counter(
            "livecart_shipment_creations_total",
            "Shipments of paid orders created at the carrier, by outcome and by path.",
            ["outcome", "via"],
            registry=registry,
        )
        # Every label of a closed list exists from the start, at zero.
        for source in IntentSource:
            self.intents_detected.labels(source=source.value)
        for reason in RejectionReason:
            self.comments_rejected.labels(reason=reason.value)
        for cause in FailureCause:
            self.comment_failures.labels(cause=cause.value)
        for result in PaymentEventResult:
            self.payment_events.labels(result=result.value)
        for outcome in CheckoutExpiryOutcome:
            self.checkout_expiries.labels(outcome=outcome.value)
        for creation in ShipmentCreationOutcome:
            for via in ShipmentPath:
                self.shipment_creations.labels(outcome=creation.value, via=via.value)

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

    def record_checkout_expiry(self, tick: CheckoutExpiryTick) -> None:
        for handled in tick.handled:
            self.checkout_expiries.labels(outcome=handled.outcome.value).inc()
            self.session_events_unpublished.inc(handled.unpublished_events)
            if handled.released:
                self.reservations_expired.inc()
            elif handled.paid:
                self.orders_paid.inc()

    def record_shipment_creation(self, created: CreatedShipment, *, via: ShipmentPath) -> None:
        """One paid order's shipment, by the path that asked for it."""
        self.shipment_creations.labels(outcome=created.outcome.value, via=via.value).inc()
        self.session_events_unpublished.inc(created.unpublished_events)

    def record_payment_events(self, tick: PaymentEventsTick) -> None:
        for handled in tick.handled:
            self.payment_events.labels(result=handled.result.value).inc()
            self.session_events_unpublished.inc(handled.unpublished_events)
            if handled.paid:
                self.orders_paid.inc()
            elif handled.paid_after_expiry:
                self.orders_paid_after_expiry.inc()
