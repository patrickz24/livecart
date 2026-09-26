"""Process-wide resources, built once by the composition root and injected into the app.

Tests inject fakes through the same types, so no test needs a real PostgreSQL
or Redis to exercise the HTTP layer.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial

from livecart_core.infrastructure.db.engine import API_POOL, create_db_engine, ping_database
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


def build_resources(settings: Settings) -> Resources:
    """Create the engine and the Redis client; nothing connects until first use."""
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
    )
