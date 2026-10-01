"""The worker's metrics as its consumer, reclaim and sweep record them, scraped from its registry.

The consumer runs ``ProcessComment`` over the fakes, so each count comes from
a real settlement: a match, a rejection, a duplicate.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from pydantic import SecretStr

from livecart_core.application.create_shipment import CreatedShipment, ShipmentCreationOutcome
from livecart_core.application.dto import QueueDelivery
from livecart_core.application.expire_reservations import ExpireReservations, SweepResult
from livecart_core.application.ports import NullIntentFallback, ServiceUnavailable
from livecart_core.application.process_comment import HandledComment, ProcessComment
from livecart_core.application.reclaim_comments import DeadLetter, Reclaimed
from livecart_core.application.sync_shipment_tracking import TrackingOutcome
from livecart_core.domain.outcomes import RejectionReason
from livecart_core.domain.state_machines import ShipmentStatus
from livecart_core.settings import Settings
from livecart_core.testing.builders import (
    DEMO_MAX_QUANTITY,
    demo_catalog,
    paid_order,
    queued_comment,
)
from livecart_core.testing.fakes import (
    FAKE_CLOCK_START,
    TEST_ORDER_TOKENS,
    FakeBuyerNotifier,
    FakeClock,
    FakeCommentQueue,
    FakeEventPublisher,
    InMemoryDatabase,
)
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY
from livecart_worker import main
from livecart_worker.consumer import CommentConsumer
from livecart_worker.metrics import FailureCause, ShipmentPath, WorkerMetrics
from livecart_worker.sweeps import sweep_once

CONSUMER = "worker-1"
# The worker reads its envelopes 2.5 s after the API queued them.
WAITED_S = 2.5


@dataclass
class Worker:
    queue: FakeCommentQueue
    consumer: CommentConsumer
    events: FakeEventPublisher

    @property
    def registry(self) -> CollectorRegistry:
        return self.consumer.metrics.registry

    def sample(self, name: str, **labels: str) -> float | None:
        return self.registry.get_sample_value(name, labels)

    async def handle(self, *texts: str) -> None:
        comments = [
            queued_comment(text, external_comment_id=f"c-{index}", author=f"buyer-{index}")
            for index, text in enumerate(texts)
        ]
        await self.queue.enqueue(comments)
        for delivery in await self.queue.receive(CONSUMER, max_messages=100, block_seconds=0):
            self.consumer.submit(delivery)
        await self.consumer.drain()


def _worker() -> Worker:
    database = InMemoryDatabase()
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    events = FakeEventPublisher()
    session = database.add_live_session()
    database.add_catalog(session.id, demo_catalog(), stock_total=10)
    process = ProcessComment(
        inbox=database.comments,
        sessions=database.live_sessions,
        orders=database.orders,
        queue=queue,
        events=events,
        notifier=FakeBuyerNotifier(),
        access_token=TEST_ORDER_TOKENS.token_for,
        fallback=NullIntentFallback(),
        clock=clock,
        lease=timedelta(seconds=30),
        db_budget=timedelta(seconds=5),
        reservation_ttl=timedelta(seconds=900),
        max_quantity=DEMO_MAX_QUANTITY,
    )
    consumer = CommentConsumer(
        queue,
        process,
        name=CONSUMER,
        batch=32,
        concurrency=16,
        deadline_s=5.0,
        wall_clock=lambda: FAKE_CLOCK_START.timestamp() + WAITED_S,
    )
    return Worker(queue, consumer, events)


async def test_intents_orders_and_rejections_are_counted_by_their_closed_labels() -> None:
    worker = _worker()

    await worker.handle("je prends B07", "SOLD Z99 x2", "bonjour", "je prends A12 M et C3")

    assert worker.sample("livecart_intents_detected_total", source="parser") == 2
    assert worker.sample("livecart_intents_detected_total", source="llm") == 0
    assert worker.sample("livecart_orders_created_total") == 2
    assert worker.sample("livecart_comments_rejected_total", reason="no_intent") == 1
    assert worker.sample("livecart_comments_rejected_total", reason="multiple_products") == 1
    # Every reason of the closed list is a series from the start.
    for reason in RejectionReason:
        assert worker.sample("livecart_comments_rejected_total", reason=reason.value) is not None


async def test_the_queue_wait_and_the_processing_time_are_observed() -> None:
    worker = _worker()

    await worker.handle("je prends B07", "je prends Z99")

    assert worker.sample("livecart_queue_wait_seconds_count") == 2
    assert worker.sample("livecart_queue_wait_seconds_sum") == pytest.approx(2 * WAITED_S)
    assert worker.sample("livecart_queue_wait_seconds_bucket", le="1.0") == 0
    assert worker.sample("livecart_queue_wait_seconds_bucket", le="5.0") == 2
    assert worker.sample("livecart_comment_processing_seconds_count") == 2


async def test_a_duplicate_counts_nothing_again() -> None:
    worker = _worker()
    await worker.handle("je prends B07")

    await worker.handle("je prends B07")

    assert worker.sample("livecart_intents_detected_total", source="parser") == 1
    assert worker.sample("livecart_orders_created_total") == 1


async def test_unpublished_session_events_are_counted() -> None:
    worker = _worker()
    worker.events.fail_next(3)

    await worker.handle("je prends B07")

    assert worker.sample("livecart_session_events_unpublished_total") == 3
    assert worker.sample("livecart_orders_created_total") == 1


class Failing:
    """A handler that fails as ``failure`` says: its cause is what the metric must show."""

    def __init__(self, failure: BaseException | None, *, hang: bool = False) -> None:
        self._failure = failure
        self._hang = hang

    async def __call__(
        self, delivery: QueueDelivery, *, consumer: str, deadline: float
    ) -> HandledComment:
        if self._hang:
            await asyncio.sleep(10)
        assert self._failure is not None
        raise self._failure


@pytest.mark.parametrize(
    ("handler", "cause"),
    [
        (Failing(None, hang=True), FailureCause.DEADLINE),
        (Failing(ServiceUnavailable("scripted")), FailureCause.TRANSIENT),
        (Failing(RuntimeError("scripted")), FailureCause.ERROR),
    ],
)
async def test_failed_deliveries_are_counted_by_cause(
    handler: Failing, cause: FailureCause
) -> None:
    queue = FakeCommentQueue(FakeClock())
    consumer = CommentConsumer(
        queue, handler, name=CONSUMER, batch=1, concurrency=1, deadline_s=0.05, retries=0
    )
    await queue.enqueue([queued_comment()])
    [delivery] = await queue.receive(CONSUMER, max_messages=1, block_seconds=0)

    consumer.submit(delivery)
    await consumer.drain()

    registry = consumer.metrics.registry
    assert registry.get_sample_value("livecart_comment_failures_total", {"cause": cause}) == 1
    others = [other for other in FailureCause if other is not cause]
    assert [
        registry.get_sample_value("livecart_comment_failures_total", {"cause": other})
        for other in others
    ] == [0, 0]
    assert registry.get_sample_value("livecart_comment_processing_seconds_count") == 1


class Reclaimer:
    def __init__(self, reclaimed: Reclaimed) -> None:
        self._reclaimed = reclaimed

    async def __call__(self, consumer: str, *, room: int) -> Reclaimed:
        return self._reclaimed


async def test_each_reclaim_counts_its_work_and_samples_the_backlog() -> None:
    queue = FakeCommentQueue(FakeClock())
    await queue.enqueue([queued_comment(external_comment_id=f"c-{n}") for n in range(3)])
    dead = await queue.receive(CONSUMER, max_messages=1, block_seconds=0)
    reclaimed = Reclaimed(dead_letters=(DeadLetter(dead[0], None),), trimmed=7)
    consumer = CommentConsumer(
        queue,
        Failing(RuntimeError("unused")),
        name=CONSUMER,
        batch=4,
        concurrency=1,
        deadline_s=1.0,
        reclaim=Reclaimer(reclaimed),
    )

    await consumer.reclaim_once()

    registry = consumer.metrics.registry
    assert registry.get_sample_value("livecart_comments_dead_lettered_total") == 1
    assert registry.get_sample_value("livecart_comments_reclaimed_total") == 0
    assert registry.get_sample_value("livecart_queue_trimmed_entries_total") == 7
    assert registry.get_sample_value("livecart_queue_backlog") == 3


class UnreadableBacklog(FakeCommentQueue):
    async def backlog(self) -> int:
        raise ServiceUnavailable("comment queue unavailable (backlog): scripted")


async def test_a_backlog_that_cannot_be_read_keeps_the_last_sample() -> None:
    consumer = CommentConsumer(
        UnreadableBacklog(FakeClock()),
        Failing(RuntimeError("unused")),
        name=CONSUMER,
        batch=4,
        concurrency=1,
        deadline_s=1.0,
        reclaim=Reclaimer(Reclaimed()),
    )
    consumer.metrics.queue_backlog.set(5)

    await consumer.reclaim_once()

    assert consumer.metrics.registry.get_sample_value("livecart_queue_backlog") == 5


async def test_each_sweep_counts_the_reservations_it_expired() -> None:
    clock = FakeClock()
    metrics = WorkerMetrics()
    expire = ExpireReservations(
        InMemoryDatabase().orders, FakeEventPublisher(), clock, budget=timedelta(seconds=5)
    )

    await sweep_once(expire, metrics)
    metrics.record_sweep(SweepResult(expired=4, batches=1, unpublished_events=2))

    assert metrics.registry.get_sample_value("livecart_reservations_expired_total") == 4
    assert metrics.registry.get_sample_value("livecart_session_events_unpublished_total") == 2


@dataclass
class ServedRegistries:
    """In place of ``MetricsServer``: which registry ``run`` served on which port, until when."""

    started: list[tuple[CollectorRegistry, int]] = field(default_factory=list)
    closed: int = 0

    def start(self, registry: CollectorRegistry, *, port: int) -> "ServedRegistries":
        self.started.append((registry, port))
        return self

    def close(self) -> None:
        self.closed += 1


async def test_run_serves_the_metrics_of_its_loops_while_they_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = ServedRegistries()
    loops: list[CollectorRegistry] = []

    async def serve(settings: Settings, stop: asyncio.Event, *, metrics: WorkerMetrics) -> None:
        loops.append(metrics.registry)
        assert served.closed == 0

    monkeypatch.setattr(main, "serve", serve)
    monkeypatch.setattr(main, "MetricsServer", served)
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@db.invalid/livecart"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        metrics_port=9321,
        _env_file=None,
    )

    await main.run(settings)

    assert served.started == [(loops[0], 9321)]
    assert served.closed == 1


def test_no_created_series_doubles_the_counters() -> None:
    text = generate_latest(WorkerMetrics().registry).decode()

    assert "livecart_orders_created_total 0.0" in text
    assert "_created{" not in text
    assert "livecart_orders_created_created" not in text


def test_every_shipment_creation_outcome_and_path_is_a_series_from_the_start() -> None:
    sample = WorkerMetrics().registry.get_sample_value

    for outcome in ShipmentCreationOutcome:
        for via in ShipmentPath:
            labels = {"outcome": outcome.value, "via": via.value}
            assert sample("livecart_shipment_creations_total", labels) == 0


async def test_a_shipment_creation_counts_its_outcome_path_and_unpublished_event() -> None:
    database, metrics = InMemoryDatabase(), WorkerMetrics()
    order = await paid_order(database)
    created = CreatedShipment(order, ShipmentCreationOutcome.CREATED, unpublished_events=1)

    metrics.record_shipment_creation(created, via=ShipmentPath.FALLBACK)

    sample = metrics.registry.get_sample_value
    labels = {"outcome": "created", "via": "fallback"}
    assert sample("livecart_shipment_creations_total", labels) == 1
    assert sample("livecart_session_events_unpublished_total") == 1


def test_every_tracking_outcome_and_shipment_status_is_a_series_from_the_start() -> None:
    sample = WorkerMetrics().registry.get_sample_value

    for outcome in TrackingOutcome:
        assert sample("livecart_shipment_tracking_total", {"outcome": outcome.value}) == 0
    for status in ShipmentStatus:
        assert sample("livecart_shipment_transitions_total", {"status": status.value}) == 0
