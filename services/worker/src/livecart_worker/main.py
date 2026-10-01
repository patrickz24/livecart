"""Composition root of the worker process, started as ``livecart-worker``.

``serve`` builds the adapters once per process (one engine, whose pool fits
``WORKER_DB_MAX_CONNECTIONS``, one Redis client) and the use cases over them,
then runs the worker's loops in one TaskGroup:

- the comment consumer (``consumer.CommentConsumer`` over ``ProcessComment``),
  whose loop also reclaims every 30 s (``ReclaimComments``): deliveries left
  idle by another consumer redelivered or dead-lettered, then the safe trim of
  the stream;
- the expiry sweep, every 5 s (``sweeps``): the reservations nobody paid in
  time expired, and their stock released; for an order whose payment is in
  flight, once the payment provider has expired the attempt's session
  (``ExpireCheckouts``), or settled as paid when the buyer paid first;
- the payment events loop, every 2 s (``payments``): the events the payment
  webhook recorded in ``processed_webhook_events``, applied to their
  attempts and orders;
- the heartbeat, which also checks that the consumer keeps going round.

``ProcessComment`` asks Claude (``livecart_worker.llm.claude``) about the
comments the parser leaves ambiguous when ``ANTHROPIC_API_KEY`` is set and
``LLM_ENABLED`` is not false, through ``LlmGuard``: the budget shared in
Redis (``RedisLlmBudget``), then one of ``LLM_SLOTS`` (4) slots, waited for
``LLM_SLOT_WAIT_S`` at most. Otherwise ``NullIntentFallback`` leaves them
unresolved, and the worker runs on the parser alone.

The consumer, the sweep and the payment events loop publish the session
events of what they commit to each session's Redis stream
(``RedisSessionEvents``), after the commit and best effort: a failure is
logged, counted in the metrics, and never undoes the commit.

The worker builds the payment provider of ``PAYMENT_PROVIDER``, as the API
does (``build_payment_provider``): the mock's sessions in the shared Redis,
or Stripe in test mode. It refuses the mock in production, and creates a
missing session again with the bridge page of ``PUBLIC_API_BASE_URL``
(``CheckoutPages``), so that its request is the API's, under the same
idempotency key.

``run`` serves the loops' metrics (``WorkerMetrics``) on ``METRICS_PORT``,
the container's internal port 9100, for as long as they run.

SIGTERM (``docker stop``) and SIGINT (Ctrl+C) set a stop event that every
loop watches: the consumer stops reading, lets the handlers that started
finish (each by its deadline), leaves the deliveries still waiting for a
handler pending, and the process releases its connections and exits with
status 0: one deadline at most, within compose's stop_grace_period. A
delivery cut short or never started is only left pending, never lost.
"""

import asyncio
import signal
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Final

import structlog
from anthropic import AsyncAnthropic

from livecart_core.application.apply_payment_events import (
    ApplyPaymentEvent,
    ProcessPaymentEvents,
)
from livecart_core.application.checkout import CheckoutPages
from livecart_core.application.expire_checkouts import ExpireCheckouts
from livecart_core.application.expire_reservations import ExpireReservations
from livecart_core.application.llm_guard import LlmGuard
from livecart_core.application.ports import Clock, IntentFallback, LlmBudget, NullIntentFallback
from livecart_core.application.process_comment import ProcessComment
from livecart_core.application.reclaim_comments import ReclaimComments
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.infrastructure.comment_queue import RedisCommentQueue
from livecart_core.infrastructure.db.comments import SqlCommentInbox
from livecart_core.infrastructure.db.engine import create_db_engine
from livecart_core.infrastructure.db.live_sessions import SqlLiveSessionRepository
from livecart_core.infrastructure.db.orders import SqlOrderRepository
from livecart_core.infrastructure.db.payments import SqlPaymentRepository
from livecart_core.infrastructure.db.webhook_events import SqlWebhookEventRegistry
from livecart_core.infrastructure.events import RedisSessionEvents
from livecart_core.infrastructure.llm_budget import RedisLlmBudget
from livecart_core.infrastructure.notifier import SimulatedBuyerNotifier
from livecart_core.infrastructure.payments import (
    build_payment_provider,
    close_payment_provider,
    refuse_mock_payments_in_production,
)
from livecart_core.infrastructure.redis_client import create_redis_client, ping_redis
from livecart_core.logging import configure_logging
from livecart_core.metrics import MetricsServer
from livecart_core.order_tokens import OrderTokens
from livecart_core.settings import LLM_MAX_RETRIES, LLM_SLOTS, Settings
from livecart_worker.consumer import (
    RECLAIM_INTERVAL_S,
    CommentConsumer,
    default_consumer_name,
)
from livecart_worker.heartbeat import HEARTBEAT_INTERVAL_S, run_heartbeat
from livecart_worker.llm.claude import AnthropicIntentFallback
from livecart_worker.metrics import WorkerMetrics
from livecart_worker.payments import (
    PAYMENT_EVENTS_BUDGET,
    PAYMENT_EVENTS_INTERVAL_S,
    run_payment_events,
)
from livecart_worker.sweeps import EXPIRY_BUDGET, EXPIRY_INTERVAL_S, run_expiry_sweep

logger = structlog.get_logger(__name__)

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
APPLICATION_NAME: Final = "livecart-worker"
# How long the consumer may go without a round of its loop before the
# heartbeat stops beating: it goes round at least every RECEIVE_BLOCK_S (2 s).
CONSUMER_STALL_S: Final = 30.0


def install_stop_handlers(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> Callable[[], None]:
    """Make SIGTERM and SIGINT set ``stop``; return a function that restores the defaults."""
    try:
        for sig in STOP_SIGNALS:
            loop.add_signal_handler(sig, stop.set)
    except NotImplementedError:
        # Windows event loops lack add_signal_handler; asyncio still wakes up on signals.
        def on_signal(_signum: int, _frame: object) -> None:
            loop.call_soon_threadsafe(stop.set)

        previous = {sig: signal.signal(sig, on_signal) for sig in STOP_SIGNALS}

        def restore_signal_handlers() -> None:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

        return restore_signal_handlers

    def remove_signal_handlers() -> None:
        for sig in STOP_SIGNALS:
            loop.remove_signal_handler(sig)

    return remove_signal_handlers


def llm_fallback(
    settings: Settings, *, budget: LlmBudget, clock: Clock
) -> tuple[IntentFallback, AsyncAnthropic | None]:
    """Claude behind its guard, with the client to close, when the fallback is on.

    The parser alone otherwise: ``NullIntentFallback``, and no client.
    """
    if not settings.llm_active or settings.anthropic_api_key is None:
        return NullIntentFallback(), None
    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        max_retries=LLM_MAX_RETRIES,
        timeout=settings.llm_call_timeout_s,
    )
    claude = AnthropicIntentFallback(
        client,
        model=settings.llm_model,
        effort=settings.llm_effort,
        refusal_fallback=settings.llm_refusal_fallback,
        call_timeout_s=settings.llm_call_timeout_s,
        max_quantity=settings.max_quantity_per_comment,
    )
    guard = LlmGuard(
        claude,
        budget,
        clock,
        model=settings.llm_model,
        prices=settings.llm_prices,
        max_calls_per_session=settings.llm_max_calls_per_session,
        daily_budget_usd=settings.llm_daily_budget_usd,
        slots=LLM_SLOTS,
        slot_wait=timedelta(seconds=settings.llm_slot_wait_s),
        call_budget=timedelta(seconds=settings.llm_call_budget_s),
    )
    return guard, client


async def serve(
    settings: Settings,
    stop: asyncio.Event,
    *,
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
    clock: Clock | None = None,
    consumer_name: str | None = None,
    reclaim_interval_s: float = RECLAIM_INTERVAL_S,
    expiry_interval_s: float = EXPIRY_INTERVAL_S,
    payment_events_interval_s: float = PAYMENT_EVENTS_INTERVAL_S,
    metrics: WorkerMetrics | None = None,
) -> None:
    """Run every worker loop until ``stop`` is set.

    ``clock`` and ``consumer_name`` default to the system clock and to
    ``<host>-<pid>``; the tests fix them, and the intervals of the loops.
    The loops record their metrics in ``metrics``, a registry of their own
    by default.
    """
    # The mock charges nothing: a production worker never settles payments with it.
    refuse_mock_payments_in_production(settings)
    clock = clock if clock is not None else SystemClock()
    metrics = metrics if metrics is not None else WorkerMetrics()
    redis = create_redis_client(settings.redis_url)
    # A stub, or a provider without its keys, stops the worker here.
    provider = build_payment_provider(settings, redis, clock)
    fallback, llm_client = llm_fallback(
        settings, budget=RedisLlmBudget(redis, prefix=settings.redis_key_prefix), clock=clock
    )
    if isinstance(fallback, LlmGuard):
        metrics.watch_llm(fallback)
    engine = create_db_engine(
        settings.database_url,
        application_name=APPLICATION_NAME,
        pool_size=settings.worker_pool.size,
        max_overflow=settings.worker_pool.max_overflow,
    )
    try:
        queue = RedisCommentQueue(redis, prefix=settings.redis_key_prefix)
        inbox = SqlCommentInbox(engine)
        tokens = OrderTokens(settings.order_token_key)
        orders = SqlOrderRepository(
            engine, currency=settings.default_currency, access_token_hash=tokens.stored_hash
        )
        # After each commit, the events of what it changed go to their session's stream.
        events = RedisSessionEvents(redis, prefix=settings.redis_key_prefix)
        process = ProcessComment(
            inbox=inbox,
            sessions=SqlLiveSessionRepository(engine),
            orders=orders,
            queue=queue,
            events=events,
            # Refused at startup in production: see livecart_core.infrastructure.notifier.
            notifier=SimulatedBuyerNotifier(
                redis,
                prefix=settings.redis_key_prefix,
                link_scheme=settings.buyer_link_scheme,
                production=settings.is_production,
            ),
            access_token=tokens.token_for,
            fallback=fallback,
            clock=clock,
            lease=settings.message_process_timeout,
            db_budget=settings.db_budget,
            reservation_ttl=settings.reservation_ttl,
            max_quantity=settings.max_quantity_per_comment,
        )
        consumer = CommentConsumer(
            queue,
            process,
            name=consumer_name if consumer_name is not None else default_consumer_name(),
            batch=settings.worker_batch,
            concurrency=settings.worker_concurrency,
            deadline_s=settings.message_process_timeout_s,
            reclaim=ReclaimComments(
                queue,
                inbox,
                clock,
                min_idle=settings.queue_reclaim_min_idle,
                max_deliveries=settings.queue_max_deliveries,
            ),
            reclaim_interval_s=reclaim_interval_s,
            metrics=metrics,
        )
        expire = ExpireReservations(orders, events, clock, budget=EXPIRY_BUDGET)
        registry = SqlWebhookEventRegistry(engine)
        apply_payment = ApplyPaymentEvent(registry, events, clock)
        payment_events = ProcessPaymentEvents(
            registry, apply_payment, clock, budget=PAYMENT_EVENTS_BUDGET
        )
        # The later phases of the sweep: the due orders with a payment in flight.
        checkouts = ExpireCheckouts(
            orders,
            SqlPaymentRepository(engine),
            provider,
            registry,
            apply_payment,
            events,
            clock,
            pages=CheckoutPages(settings.public_api_base_url),
            budget=EXPIRY_BUDGET,
        )
        async with asyncio.TaskGroup() as loops:
            loops.create_task(consumer.run(stop))
            loops.create_task(
                run_expiry_sweep(
                    expire,
                    stop,
                    checkouts=checkouts,
                    interval_s=expiry_interval_s,
                    metrics=metrics,
                )
            )
            loops.create_task(
                run_payment_events(
                    payment_events,
                    stop,
                    interval_s=payment_events_interval_s,
                    metrics=metrics,
                )
            )
            loops.create_task(
                run_heartbeat(
                    ping=partial(ping_redis, redis),
                    path=settings.worker_heartbeat_file,
                    stop=stop,
                    interval_s=heartbeat_interval_s,
                    progressing=partial(consumer.progressed_within, CONSUMER_STALL_S),
                )
            )
    finally:
        await engine.dispose()
        await close_payment_provider(provider)
        await redis.aclose()
        if llm_client is not None:
            await llm_client.close()


async def run(settings: Settings) -> None:
    stop = asyncio.Event()
    restore_signal_handlers = install_stop_handlers(asyncio.get_running_loop(), stop)
    metrics = WorkerMetrics()
    server = None
    if settings.metrics_port is not None:
        server = MetricsServer.start(metrics.registry, port=settings.metrics_port)
    logger.info(
        "worker_started",
        metrics_port=settings.metrics_port,
        llm_model=settings.llm_model if settings.llm_active else None,
    )
    try:
        await serve(settings, stop, metrics=metrics)
    finally:
        if server is not None:
            server.close()
        restore_signal_handlers()
        logger.info("worker_stopped")


def main() -> None:
    settings = Settings()
    configure_logging(service="worker", level=settings.log_level)
    asyncio.run(run(settings))
