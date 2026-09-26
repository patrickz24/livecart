"""pytest fixtures for integration tests, registered for every member by the root conftest.py.

They target the compose PostgreSQL and Redis published on 127.0.0.1, through
the TEST_* variables. Only tests marked ``integration`` request them, so the
unit suite never needs Docker. Every fixture goes through
``load_integration_settings``, which refuses a PostgreSQL database not named
``*_test`` and a Redis database other than 15; ``redis_key_prefix`` gives each
test its own Redis keys, and ``clean_database`` empty tables at the head
revision.
"""

import secrets
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.ports import CommentQueue, IdempotencyStore, RateLimiter
from livecart_core.domain.reference import RandomSource
from livecart_core.infrastructure.comment_queue import RedisCommentQueue
from livecart_core.infrastructure.db.engine import create_db_engine
from livecart_core.infrastructure.idempotency import RedisIdempotencyStore
from livecart_core.infrastructure.ratelimit import RedisRateLimiter
from livecart_core.infrastructure.redis_client import create_redis_client
from livecart_core.testing.backends import (
    AuthBackend,
    Backend,
    BackendFactory,
    InMemoryBackend,
    PostgresBackend,
    ReservationBackend,
)
from livecart_core.testing.database import (
    IntegrationSettings,
    IntegrationSettingsError,
    UnsafeTestDatabaseError,
    load_integration_settings,
    unreachable_servers,
)
from livecart_core.testing.fakes import (
    FakeClock,
    FakeCommentQueue,
    InMemoryIdempotencyStore,
    InMemoryRateLimiter,
)
from livecart_core.testing.postgres import migrate_to_head, truncate_all
from livecart_core.testing.redis_keys import delete_keys_with_prefix, new_test_key_prefix


@pytest.fixture(scope="session")
def integration_settings() -> IntegrationSettings:
    try:
        settings = load_integration_settings()
    except (IntegrationSettingsError, UnsafeTestDatabaseError) as exc:
        pytest.fail(str(exc), pytrace=False)
    if down := unreachable_servers(settings):
        pytest.fail(
            f"cannot reach {', '.join(down)}: start the test servers with "
            "`docker compose up -d --wait postgres redis`, or deselect integration tests "
            "with -m 'not integration'",
            pytrace=False,
        )
    return settings


@pytest.fixture
async def app_engine(integration_settings: IntegrationSettings) -> AsyncIterator[AsyncEngine]:
    """Engine connected as livecart_app, the role the code under test runs as."""
    engine = create_db_engine(
        integration_settings.database_url,
        application_name="livecart-tests",
        pool_size=2,
        max_overflow=0,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def migrator_engine(integration_settings: IntegrationSettings) -> AsyncIterator[AsyncEngine]:
    """Engine connected as livecart_migrator, for schema setup and cleanup."""
    engine = create_db_engine(
        integration_settings.migrator_url,
        application_name="livecart-tests-migrator",
        pool_size=1,
        max_overflow=0,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_schema(integration_settings: IntegrationSettings) -> None:
    """livecart_test at the head revision, migrated once per session.

    Synchronous, because the Alembic environment runs its own event loop. A
    migration test that goes down to base in the middle of the session goes
    back up to head before it ends.
    """
    migrate_to_head(integration_settings.migrator_url)


@pytest.fixture
async def clean_database(database_schema: None, migrator_engine: AsyncEngine) -> None:
    """Every table of the schema, empty: earlier tests' rows are truncated as the migrator."""
    await truncate_all(migrator_engine)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def make_backend(request: pytest.FixtureRequest) -> BackendFactory:
    """Builds the backend of this run of a contract scenario: the fakes, then PostgreSQL.

    The PostgreSQL run is marked ``integration``, so ``-m "not integration"``
    keeps the in-memory run only. It starts from empty tables.
    """
    if request.param == "memory":
        return InMemoryBackend
    request.getfixturevalue("clean_database")
    app_engine: AsyncEngine = request.getfixturevalue("app_engine")
    migrator_engine: AsyncEngine = request.getfixturevalue("migrator_engine")

    def postgres(*, random_source: RandomSource = secrets) -> Backend:
        return PostgresBackend(app_engine, migrator_engine, random_source=random_source)

    return postgres


@pytest.fixture
def backend(make_backend: BackendFactory) -> ReservationBackend:
    """The backend of this run of a contract scenario, drawing references at random."""
    return make_backend()


@pytest.fixture
def auth_backend(make_backend: BackendFactory) -> AuthBackend:
    """The seller and refresh token repositories of this run of a contract scenario."""
    return make_backend()


@pytest.fixture
async def redis_client(integration_settings: IntegrationSettings) -> AsyncIterator[Redis]:
    """Client of Redis database 15 (TEST_REDIS_URL, checked by the settings guard)."""
    client = create_redis_client(integration_settings.redis_url)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def redis_key_prefix(redis_client: Redis) -> AsyncIterator[str]:
    """A key prefix of this test alone; every key under it is deleted when the test ends."""
    prefix = new_test_key_prefix()
    try:
        yield prefix
    finally:
        await delete_keys_with_prefix(redis_client, prefix)


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration)])
def idempotency_store(request: pytest.FixtureRequest) -> IdempotencyStore:
    """The store of idempotent answers of this run of a contract scenario: the fake, then Redis."""
    if request.param == "memory":
        return InMemoryIdempotencyStore()
    client: Redis = request.getfixturevalue("redis_client")
    prefix: str = request.getfixturevalue("redis_key_prefix")
    return RedisIdempotencyStore(client, prefix=prefix)


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration)])
def rate_limiter(request: pytest.FixtureRequest) -> RateLimiter:
    """The fixed-window counters of this run of a contract scenario: the fake, then Redis.

    The fake's clock stands still, as Redis's does within the short scenarios.
    """
    if request.param == "memory":
        return InMemoryRateLimiter(FakeClock())
    client: Redis = request.getfixturevalue("redis_client")
    prefix: str = request.getfixturevalue("redis_key_prefix")
    return RedisRateLimiter(client, prefix=prefix)


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration)])
def comment_queue(request: pytest.FixtureRequest) -> CommentQueue:
    """The comment queue of this run of a contract scenario: the fake, then Redis Streams.

    The fake's clock stands still, so the scenarios reclaim with a minimum
    idle time of 0 (every pending comment) or of minutes (none yet), which
    Redis agrees with at once.
    """
    if request.param == "memory":
        return FakeCommentQueue(FakeClock())
    client: Redis = request.getfixturevalue("redis_client")
    prefix: str = request.getfixturevalue("redis_key_prefix")
    return RedisCommentQueue(client, prefix=prefix)
