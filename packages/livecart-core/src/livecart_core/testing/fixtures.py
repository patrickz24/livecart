"""pytest fixtures for integration tests, registered for every member by the root conftest.py.

They target the compose PostgreSQL and Redis published on 127.0.0.1, through
the TEST_* variables. Only tests marked ``integration`` request them, so the
unit suite never needs Docker. Every fixture goes through
``load_integration_settings``, which refuses a PostgreSQL database not named
``*_test`` and a Redis database other than 15; ``redis_key_prefix`` gives each
test its own Redis keys, and ``clean_database`` empty tables at the head
revision.
"""

import asyncio
import contextlib
import json
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx2
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.dto import (
    BuyerNotification,
    CheckoutSession,
    PaymentEvent,
    PaymentProviderName,
    Platform,
)
from livecart_core.application.ports import (
    BuyerNotifier,
    CommentQueue,
    IdempotencyStore,
    LlmBudget,
    PaymentProvider,
    RateLimiter,
)
from livecart_core.domain.reference import OrderReference, RandomSource
from livecart_core.infrastructure.comment_queue import RedisCommentQueue
from livecart_core.infrastructure.db.engine import WORKER_POOL, create_db_engine
from livecart_core.infrastructure.events import EVENT_RETENTION, RedisSessionEvents
from livecart_core.infrastructure.idempotency import RedisIdempotencyStore
from livecart_core.infrastructure.llm_budget import RedisLlmBudget
from livecart_core.infrastructure.notifier import SimulatedBuyerNotifier
from livecart_core.infrastructure.payments.mock import MockPaymentProvider
from livecart_core.infrastructure.payments.stripe import Httpx2Client, StripePaymentProvider
from livecart_core.infrastructure.ratelimit import RedisRateLimiter
from livecart_core.infrastructure.redis_client import create_redis_client
from livecart_core.testing.backends import (
    AuthBackend,
    Backend,
    BackendFactory,
    EventsBackend,
    InMemoryBackend,
    NotifierBackend,
    PaymentProviderBackend,
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
    FAKE_CLOCK_START,
    FakeBuyerNotifier,
    FakeClock,
    FakeCommentQueue,
    FakePaymentProvider,
    InMemoryIdempotencyStore,
    InMemoryLlmBudget,
    InMemoryRateLimiter,
    InMemorySessionEvents,
)
from livecart_core.testing.keys import (
    TEST_MOCK_PAYMENT_WEBHOOK_SECRET,
    TEST_STRIPE_SECRET_KEY,
    TEST_STRIPE_WEBHOOK_SECRET,
)
from livecart_core.testing.postgres import migrate_to_head, truncate_all
from livecart_core.testing.redis_keys import delete_keys_with_prefix, new_test_key_prefix
from livecart_core.testing.stripe_api import FakeStripeApi


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


@pytest.fixture
async def worker_engine(integration_settings: IntegrationSettings) -> AsyncIterator[AsyncEngine]:
    """livecart_app through the worker's pool: 20 connections and no overflow, an exact bound.

    For the concurrency tests, whose probe (``testing.pools``) checks that
    every connection was in use at once.
    """
    engine = create_db_engine(
        integration_settings.database_url,
        application_name="livecart-tests-concurrency",
        pool_size=WORKER_POOL.size,
        max_overflow=0,
        # A thousand callers queue for twenty connections; none may give up.
        pool_timeout_s=120,
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


@pytest.fixture
def postgres_backend(
    clean_database: None, worker_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> PostgresBackend:
    """The SQL adapters on empty tables, through the worker's pool."""
    return PostgresBackend(worker_engine, migrator_engine)


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
def llm_budget(request: pytest.FixtureRequest) -> LlmBudget:
    """The LLM budget of this run of a contract scenario: the fake, then Redis."""
    if request.param == "memory":
        return InMemoryLlmBudget()
    client: Redis = request.getfixturevalue("redis_client")
    prefix: str = request.getfixturevalue("redis_key_prefix")
    return RedisLlmBudget(client, prefix=prefix)


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


# The origin the mock under test builds the URLs of its hosted pages from.
TEST_PUBLIC_API_BASE_URL = "http://localhost:8080"
# The retention of the Redis streams under test: short, so that it passes within a test.
TEST_EVENT_RETENTION = timedelta(milliseconds=200)


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration)])
def session_events(request: pytest.FixtureRequest) -> EventsBackend:
    """The session streams of this run of a contract scenario: the fake, then Redis Streams.

    The fake's clock jumps past its 15 minutes of retention; Redis's retention
    is shortened to 200 ms, trimmed exactly, and waited out.
    """
    if request.param == "memory":
        clock = FakeClock()
        memory = InMemorySessionEvents(clock, retention=EVENT_RETENTION)

        async def jump_past_retention() -> None:
            clock.advance(EVENT_RETENTION + timedelta(seconds=1))

        async def forget(session_id: UUID) -> None:
            memory.expire(session_id)

        return EventsBackend(memory, jump_past_retention, forget)
    client: Redis = request.getfixturevalue("redis_client")
    prefix: str = request.getfixturevalue("redis_key_prefix")
    redis = RedisSessionEvents(
        client, prefix=prefix, retention=TEST_EVENT_RETENTION, approximate_trim=False
    )

    async def wait_past_retention() -> None:
        await asyncio.sleep(2 * TEST_EVENT_RETENTION.total_seconds())

    async def delete(session_id: UUID) -> None:
        await client.delete(redis.stream_key(session_id))

    return EventsBackend(redis, wait_past_retention, delete)


# A Redis nobody listens on: every command fails before it reaches a server.
UNREACHABLE_REDIS_URL = SecretStr("redis://127.0.0.1:1/15")


@pytest.fixture
async def unreachable_redis_client() -> AsyncIterator[Redis]:
    """A client of a closed port, for the scenarios of a Redis that is down."""
    client = create_redis_client(UNREACHABLE_REDIS_URL)
    try:
        yield client
    finally:
        await client.aclose()


def _fake_payments() -> PaymentProviderBackend:
    fake = FakePaymentProvider()
    forger = FakePaymentProvider(key=b"a-key-the-forger-guessed")

    async def pay(provider_ref: str) -> CheckoutSession:
        with contextlib.suppress(ValueError):
            return fake.complete(provider_ref)
        return await fake.get_checkout(provider_ref)

    def unreachable() -> PaymentProvider:
        fake.fail_next(10)
        return fake

    def foreign() -> tuple[dict[str, str], bytes]:
        return fake.signed_webhook(_OTHER_PROVIDER)

    return PaymentProviderBackend(
        fake, pay, fake.signed_webhook, forger.signed_webhook, foreign, unreachable
    )


# A verified body of another provider: no event of the provider under test.
_OTHER_PROVIDER = PaymentEvent(
    provider=PaymentProviderName.STRIPE,
    event_id="evt_other",
    event_type="checkout.session.completed",
    occurred_at=FAKE_CLOCK_START,
)


async def _no_backoff(_seconds: float) -> None:
    return None


def _stripe_payments() -> PaymentProviderBackend:
    api = FakeStripeApi()

    def stripe_over(transport: httpx2.AsyncBaseTransport) -> StripePaymentProvider:
        return StripePaymentProvider(
            secret_key=TEST_STRIPE_SECRET_KEY,
            webhook_secret=TEST_STRIPE_WEBHOOK_SECRET,
            http_client=Httpx2Client(transport=transport, sleep=_no_backoff),
        )

    provider = stripe_over(api.transport)

    async def pay(provider_ref: str) -> CheckoutSession:
        api.pay(provider_ref)
        return await provider.get_checkout(provider_ref)

    def signed(event: PaymentEvent) -> tuple[dict[str, str], bytes]:
        return api.signed(event, TEST_STRIPE_WEBHOOK_SECRET)

    def forged(event: PaymentEvent) -> tuple[dict[str, str], bytes]:
        return api.signed(event, SecretStr("whsec_a-secret-the-forger-guessed"))

    def foreign() -> tuple[dict[str, str], bytes]:
        # A thin event notification of the v2 API: no snapshot event of a session.
        body = json.dumps({"id": "evt_thin", "object": "v2.core.event"}).encode()
        return api.sign(body, TEST_STRIPE_WEBHOOK_SECRET), body

    def unreachable() -> PaymentProvider:
        down = FakeStripeApi()
        down.down = True
        return stripe_over(down.transport)

    return PaymentProviderBackend(provider, pay, signed, forged, foreign, unreachable)


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration), "stripe"])
def payment_provider(request: pytest.FixtureRequest) -> PaymentProviderBackend:
    """The payment provider of this run of a contract scenario: the fake, the mock, then Stripe.

    The fake and the mock on Redis run on a clock that stands still, as the
    mock's signatures are dated by it; the unreachable mock is built over a
    closed port, under the same prefix. Stripe runs its real adapter, and the
    SDK, over ``FakeStripeApi``: no network, so no Docker either.
    """
    if request.param == "memory":
        return _fake_payments()
    if request.param == "stripe":
        return _stripe_payments()
    client: Redis = request.getfixturevalue("redis_client")
    unreachable_client: Redis = request.getfixturevalue("unreachable_redis_client")
    prefix: str = request.getfixturevalue("redis_key_prefix")
    clock = FakeClock()

    def mock(
        redis: Redis, secret: SecretStr = TEST_MOCK_PAYMENT_WEBHOOK_SECRET
    ) -> MockPaymentProvider:
        return MockPaymentProvider(
            redis,
            prefix=prefix,
            secret=secret,
            clock=clock,
            public_base_url=TEST_PUBLIC_API_BASE_URL,
        )

    provider = mock(client)
    forger = mock(client, SecretStr("a-key-the-forger-guessed-" * 2))

    async def pay(provider_ref: str) -> CheckoutSession:
        return (await provider.pay(provider_ref)).session

    return PaymentProviderBackend(
        provider,
        pay,
        provider.signed_webhook,
        forger.signed_webhook,
        lambda: provider.signed_webhook(_OTHER_PROVIDER),
        lambda: mock(unreachable_client),
    )


def _streamed_notice(fields: dict[bytes, bytes]) -> BuyerNotification:
    """A notice read back from an entry of the development stream."""
    text = {key.decode(): value.decode() for key, value in fields.items()}
    return BuyerNotification(
        order_id=UUID(text["order_id"]),
        platform=Platform(text["platform"]),
        recipient=text["recipient"],
        reference=OrderReference(text["reference"]),
        access_token=text["token"],
    )


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration)])
def buyer_notifier(request: pytest.FixtureRequest) -> NotifierBackend:
    """The buyer notifier of this run of a contract scenario: the fake, then the Redis stream.

    The fake fails its next notice when asked; on Redis, the failing notifier
    writes to a closed port under the same key prefix, so that the stream
    read back is the one it would have written.
    """
    if request.param == "memory":
        fake = FakeBuyerNotifier()

        async def sent() -> list[BuyerNotification]:
            return list(fake.sent)

        def failing_fake() -> BuyerNotifier:
            fake.fail_next()
            return fake

        return NotifierBackend(fake, sent, failing_fake)
    client: Redis = request.getfixturevalue("redis_client")
    unreachable: Redis = request.getfixturevalue("unreachable_redis_client")
    prefix: str = request.getfixturevalue("redis_key_prefix")
    notifier = SimulatedBuyerNotifier(
        client, prefix=prefix, link_scheme="livecart", production=False
    )

    async def streamed() -> list[BuyerNotification]:
        entries: Any = await client.xrange(notifier.stream_key)
        return [_streamed_notice(fields) for _, fields in entries]

    def failing_redis() -> BuyerNotifier:
        return SimulatedBuyerNotifier(
            unreachable, prefix=prefix, link_scheme="livecart", production=False
        )

    return NotifierBackend(notifier, streamed, failing_redis)
