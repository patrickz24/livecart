"""Process-wide resources, built once by the composition root and injected into the app.

Tests inject fakes through the same types (``livecart_api.testing``), so no
unit test needs a real PostgreSQL or Redis to exercise the HTTP layer; the
integration tests inject the SQL adapters on livecart_test instead.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial

from livecart_core.application.dto import Platform
from livecart_core.application.ports import (
    Clock,
    CommentQueue,
    IdempotencyStore,
    LiveSessionActivity,
    LiveSessionRepository,
    OrderLookup,
    PasswordHasher,
    PlatformAdapter,
    ProductStockRepository,
    RateLimiter,
    RefreshTokenRepository,
    SellerRepository,
    SessionEventLog,
    SessionEventStream,
)
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.infrastructure.comment_queue import RedisCommentQueue
from livecart_core.infrastructure.db.activity import SqlLiveSessionActivity
from livecart_core.infrastructure.db.engine import API_POOL, create_db_engine, ping_database
from livecart_core.infrastructure.db.live_sessions import SqlLiveSessionRepository
from livecart_core.infrastructure.db.orders import SqlOrderRepository
from livecart_core.infrastructure.db.products import SqlProductStockRepository
from livecart_core.infrastructure.db.refresh_tokens import SqlRefreshTokenRepository
from livecart_core.infrastructure.db.sellers import SqlSellerRepository
from livecart_core.infrastructure.events import RedisSessionEvents
from livecart_core.infrastructure.idempotency import RedisIdempotencyStore
from livecart_core.infrastructure.passwords import Argon2PasswordHasher
from livecart_core.infrastructure.platforms import build_platform_adapters
from livecart_core.infrastructure.ratelimit import RedisRateLimiter
from livecart_core.infrastructure.redis_client import create_redis_client, ping_redis
from livecart_core.order_tokens import OrderTokens
from livecart_core.settings import Settings


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """A dependency probed by ``/ready``: ``probe`` raises when it is unavailable."""

    name: str
    probe: Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Resources:
    readiness_checks: Sequence[ReadinessCheck]
    # Releases connections and pools; awaited once when the app shuts down.
    aclose: Callable[[], Awaitable[None]]
    sellers: SellerRepository
    refresh_tokens: RefreshTokenRepository
    passwords: PasswordHasher
    clock: Clock
    live_sessions: LiveSessionRepository
    products: ProductStockRepository
    idempotency: IdempotencyStore
    orders: OrderLookup
    rate_limiter: RateLimiter
    # The comment webhook's adapter of each enabled platform, and the queue it feeds.
    platforms: Mapping[Platform, PlatformAdapter]
    comment_queue: CommentQueue
    # The snapshot and the streams of the sessions' events (SSE), and the
    # publisher of a session's start and end.
    activity: LiveSessionActivity
    session_events: SessionEventLog
    # What the SSE streams read the events through: on a Redis client of their
    # own in build_resources, so that the streams, each blocked in XREAD, never
    # take a connection from the webhook, the login or /ready.
    event_streams: SessionEventStream


def build_resources(settings: Settings) -> Resources:
    """Create the engine, the Redis clients and the adapters; nothing connects until first use.

    The password hasher computes its dummy hash here, once: one argon2 run
    when the process starts. The platform adapters are built here too, so an
    enabled stub or a missing webhook secret stops the process before it
    listens (``livecart_core.infrastructure.platforms``).

    Two Redis clients: the shared one, of redis-py's default 100
    connections, for every short command (the rate limits, the queue,
    idempotency, the events published, /ready), and the event streams' own,
    of ``SSE_MAX_STREAMS`` connections. A stream holds one connection at
    most at a time, since its reads follow one another and each gives its
    connection back before the next, and ``StreamSlots`` caps the streams:
    their pool never runs out before the slots do. On the shared pool, 100
    streams in ``XREAD BLOCK`` would take every connection, and every other
    use of Redis would fail with ``MaxConnectionsError`` until they closed.
    """
    clock = SystemClock()
    platforms = build_platform_adapters(settings, clock)
    engine = create_db_engine(
        settings.database_url,
        application_name="livecart-api",
        pool_size=API_POOL.size,
        max_overflow=API_POOL.max_overflow,
    )
    redis = create_redis_client(settings.redis_url)
    stream_redis = create_redis_client(settings.redis_url, max_connections=settings.sse_max_streams)

    async def aclose() -> None:
        try:
            try:
                await redis.aclose()
            finally:
                await stream_redis.aclose()
        finally:
            await engine.dispose()

    return Resources(
        readiness_checks=(
            ReadinessCheck("postgres", partial(ping_database, engine)),
            ReadinessCheck("redis", partial(ping_redis, redis)),
        ),
        aclose=aclose,
        sellers=SqlSellerRepository(engine),
        refresh_tokens=SqlRefreshTokenRepository(engine),
        passwords=Argon2PasswordHasher(),
        clock=clock,
        live_sessions=SqlLiveSessionRepository(engine),
        products=SqlProductStockRepository(engine, currency=settings.default_currency),
        idempotency=RedisIdempotencyStore(redis, prefix=settings.redis_key_prefix),
        orders=SqlOrderRepository(
            engine,
            currency=settings.default_currency,
            access_token_hash=OrderTokens(settings.order_token_key).stored_hash,
        ),
        rate_limiter=RedisRateLimiter(redis, prefix=settings.redis_key_prefix),
        platforms=platforms,
        comment_queue=RedisCommentQueue(redis, prefix=settings.redis_key_prefix),
        activity=SqlLiveSessionActivity(engine, currency=settings.default_currency),
        session_events=RedisSessionEvents(redis, prefix=settings.redis_key_prefix),
        event_streams=RedisSessionEvents(stream_redis, prefix=settings.redis_key_prefix),
    )
