"""Process-wide resources, built once by the composition root and injected into the app.

Tests inject fakes through the same types (``livecart_api.testing``), so no
unit test needs a real PostgreSQL or Redis to exercise the HTTP layer; the
integration tests inject the SQL adapters on livecart_test instead.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial

from livecart_core.application.ports import (
    Clock,
    IdempotencyStore,
    LiveSessionRepository,
    PasswordHasher,
    ProductStockRepository,
    RefreshTokenRepository,
    SellerRepository,
)
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.infrastructure.db.engine import API_POOL, create_db_engine, ping_database
from livecart_core.infrastructure.db.live_sessions import SqlLiveSessionRepository
from livecart_core.infrastructure.db.products import SqlProductStockRepository
from livecart_core.infrastructure.db.refresh_tokens import SqlRefreshTokenRepository
from livecart_core.infrastructure.db.sellers import SqlSellerRepository
from livecart_core.infrastructure.idempotency import RedisIdempotencyStore
from livecart_core.infrastructure.passwords import Argon2PasswordHasher
from livecart_core.infrastructure.redis_client import create_redis_client, ping_redis
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


def build_resources(settings: Settings) -> Resources:
    """Create the engine, the Redis client and the adapters; nothing connects until first use.

    The password hasher computes its dummy hash here, once: one argon2 run
    when the process starts.
    """
    engine = create_db_engine(
        settings.database_url,
        application_name="livecart-api",
        pool_size=API_POOL.size,
        max_overflow=API_POOL.max_overflow,
    )
    redis = create_redis_client(settings.redis_url)

    async def aclose() -> None:
        try:
            await redis.aclose()
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
        clock=SystemClock(),
        live_sessions=SqlLiveSessionRepository(engine),
        products=SqlProductStockRepository(engine, currency=settings.default_currency),
        idempotency=RedisIdempotencyStore(redis, prefix=settings.redis_key_prefix),
    )
