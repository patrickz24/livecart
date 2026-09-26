"""A burst of ambiguous comments and a slow model, through the consumer, ProcessComment, the guard.

Sixteen deliveries at once, one handler each, and four slots: the model
answers in 0.5 s, and a comment waits 0.75 s at most for a slot. The first
two rounds of four get an answer; the comments still waiting when their
wait ends are settled as ``llm_busy``, unresolved, and acknowledged. None
reaches its deadline, none is left for a reclaim or a dead letter. The
times are the defaults (7 s of slot wait, 30 s of deadline) scaled down,
their ratios kept.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from livecart_core.application.dto import FallbackReceipt, FallbackRequest, LlmPrice
from livecart_core.application.llm_guard import LlmGuard
from livecart_core.application.process_comment import ProcessComment
from livecart_core.domain.outcomes import LlmOutcome, RejectionReason
from livecart_core.testing.builders import DEMO_MAX_QUANTITY, demo_catalog, queued_comment
from livecart_core.testing.fakes import (
    TEST_ORDER_TOKENS,
    FakeBuyerNotifier,
    FakeClock,
    FakeCommentQueue,
    FakeEventPublisher,
    FakeIntentFallback,
    InMemoryDatabase,
    InMemoryLlmBudget,
    ok_receipt,
)
from livecart_worker.consumer import CommentConsumer
from livecart_worker.metrics import FailureCause, WorkerMetrics

COMMENTS = 16
SLOTS = 4
MODEL_ANSWERS_IN_S = 0.5
SLOT_WAIT_S = 0.75
DEADLINE_S = 3.0
MODEL = "claude-opus-5"


class _SlowModel(FakeIntentFallback):
    """Answers every call after the same delay, and notes how many ran at once."""

    running = 0
    most_running = 0

    async def resolve(self, request: FallbackRequest) -> FallbackReceipt:
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            return await super().resolve(request)
        finally:
            self.running -= 1


@dataclass
class Burst:
    queue: FakeCommentQueue
    model: _SlowModel
    guard: LlmGuard
    consumer: CommentConsumer
    database: InMemoryDatabase

    def sample(self, name: str, **labels: str) -> float | None:
        return self.consumer.metrics.registry.get_sample_value(name, labels)


def _burst() -> Burst:
    database = InMemoryDatabase()
    clock = FakeClock()
    queue = FakeCommentQueue(clock)
    session = database.add_live_session()
    database.add_catalog(session.id, demo_catalog(), stock_total=99)
    model = _SlowModel([ok_receipt("B07", quantity=2)] * COMMENTS, delay_seconds=MODEL_ANSWERS_IN_S)
    guard = LlmGuard(
        model,
        InMemoryLlmBudget(),
        clock,
        model=MODEL,
        prices={MODEL: LlmPrice(input_per_mtok=Decimal(5), output_per_mtok=Decimal(25))},
        max_calls_per_session=200,
        daily_budget_usd=Decimal("1.00"),
        slots=SLOTS,
        slot_wait=timedelta(seconds=SLOT_WAIT_S),
        call_budget=timedelta(seconds=1.8),
    )
    process = ProcessComment(
        inbox=database.comments,
        sessions=database.live_sessions,
        orders=database.orders,
        queue=queue,
        events=FakeEventPublisher(),
        notifier=FakeBuyerNotifier(),
        access_token=TEST_ORDER_TOKENS.token_for,
        fallback=guard,
        clock=clock,
        lease=timedelta(seconds=DEADLINE_S),
        db_budget=timedelta(seconds=0.5),
        reservation_ttl=timedelta(seconds=900),
        max_quantity=DEMO_MAX_QUANTITY,
    )
    metrics = WorkerMetrics()
    metrics.watch_llm(guard)
    consumer = CommentConsumer(
        queue,
        process,
        name="worker-1",
        batch=32,
        concurrency=COMMENTS,
        deadline_s=DEADLINE_S,
        metrics=metrics,
    )
    return Burst(queue, model, guard, consumer, database)


async def test_a_burst_of_ambiguous_comments_ends_without_a_timeout_or_a_dead_letter() -> None:
    burst = _burst()
    # "deux": a quantity in words, which the parser leaves to the fallback.
    await burst.queue.enqueue(
        [
            queued_comment("je prends deux B07", external_comment_id=f"c-{n}", author=f"b-{n}")
            for n in range(COMMENTS)
        ]
    )
    for delivery in await burst.queue.receive("worker-1", max_messages=COMMENTS, block_seconds=0):
        burst.consumer.submit(delivery)

    await burst.consumer.drain()

    # Every delivery settled and acknowledged, within its deadline.
    assert len(burst.queue.acknowledged) == COMMENTS
    assert burst.queue.pending == burst.queue.dead_letters == []
    for cause in FailureCause:
        assert burst.sample("livecart_comment_failures_total", cause=cause.value) == 0
    # Never more calls at once than the slots; the excess was busy, not late.
    assert burst.model.most_running == SLOTS
    answered = burst.guard.outcomes[LlmOutcome.OK]
    busy = burst.guard.outcomes[LlmOutcome.LLM_BUSY]
    assert answered + busy == COMMENTS
    # Two rounds fit the wait on an idle machine; one at least fits it anywhere.
    assert SLOTS <= answered <= 2 * SLOTS
    assert busy >= COMMENTS - 2 * SLOTS
    assert len(burst.database.tables.orders) == answered
    unresolved = RejectionReason.AMBIGUOUS_UNRESOLVED.value
    assert burst.sample("livecart_comments_rejected_total", reason=unresolved) == busy
    assert burst.sample("livecart_llm_outcomes_total", outcome="llm_busy") == busy
