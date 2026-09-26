import uuid
from dataclasses import replace

import pytest
import structlog
from pydantic import SecretStr

from livecart_api.app import create_app
from livecart_api.resources import ReadinessCheck, build_resources
from livecart_api.testing import TEST_HOST, make_app, memory_resources, open_client
from livecart_core.application.dto import Platform
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.infrastructure.comment_queue import RedisCommentQueue
from livecart_core.infrastructure.db.live_sessions import SqlLiveSessionRepository
from livecart_core.infrastructure.db.orders import SqlOrderRepository
from livecart_core.infrastructure.db.products import SqlProductStockRepository
from livecart_core.infrastructure.db.refresh_tokens import SqlRefreshTokenRepository
from livecart_core.infrastructure.db.sellers import SqlSellerRepository
from livecart_core.infrastructure.idempotency import RedisIdempotencyStore
from livecart_core.infrastructure.passwords import Argon2PasswordHasher
from livecart_core.infrastructure.platforms.simulator import SimulatorPlatformAdapter
from livecart_core.infrastructure.ratelimit import RedisRateLimiter
from livecart_core.settings import AppEnv, Settings
from livecart_core.testing.fakes import InMemoryDatabase
from livecart_core.testing.keys import (
    TEST_COMMENT_WEBHOOK_SECRET,
    TEST_JWT_SECRET,
    TEST_ORDER_TOKEN_KEY,
)


async def test_a_request_id_is_generated_and_returned() -> None:
    async with open_client(make_app()) as client:
        response = await client.get("/health")

    assert uuid.UUID(response.headers["X-Request-ID"])


async def test_a_valid_incoming_request_id_is_kept() -> None:
    incoming = uuid.uuid4().hex

    async with open_client(make_app()) as client:
        response = await client.get("/health", headers={"X-Request-ID": incoming})

    assert response.headers["X-Request-ID"] == incoming


async def test_an_invalid_incoming_request_id_is_replaced() -> None:
    async with open_client(make_app()) as client:
        response = await client.get("/health", headers={"X-Request-ID": "forged id"})

    assert response.headers["X-Request-ID"] != "forged id"
    assert uuid.UUID(response.headers["X-Request-ID"])


async def test_the_request_id_is_bound_to_the_log_context() -> None:
    seen: list[object] = []

    async def probe() -> None:
        seen.append(structlog.contextvars.get_contextvars().get("request_id"))

    async with open_client(make_app([ReadinessCheck("postgres", probe)])) as client:
        response = await client.get("/ready")

    assert seen == [response.headers["X-Request-ID"]]
    assert "request_id" not in structlog.contextvars.get_contextvars()


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
async def test_docs_are_served_in_development(path: str) -> None:
    async with open_client(make_app(app_env=AppEnv.DEVELOPMENT)) as client:
        response = await client.get(path)

    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_docs_are_disabled_in_production(path: str) -> None:
    async with open_client(make_app(app_env=AppEnv.PRODUCTION)) as client:
        response = await client.get(path)

    assert response.status_code == 404


def test_openapi_documents_both_probes() -> None:
    schema = make_app(app_env=AppEnv.PRODUCTION).openapi()

    health, ready = schema["paths"]["/health"]["get"], schema["paths"]["/ready"]["get"]
    assert health["operationId"] == "get_health"
    assert ready["operationId"] == "get_readiness"
    assert set(ready["responses"]) == {"200", "503"}
    assert {"HealthStatus", "ReadinessStatus"} <= set(schema["components"]["schemas"])


async def test_shutdown_closes_the_injected_resources() -> None:
    closed: list[bool] = []

    async def aclose() -> None:
        closed.append(True)

    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@db.invalid/livecart"),
        redis_url=SecretStr("redis://redis.invalid:6379/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        comment_webhook_secret_simulator=TEST_COMMENT_WEBHOOK_SECRET,
        _env_file=None,
    )
    resources = replace(memory_resources(InMemoryDatabase()), aclose=aclose)
    app = create_app(settings, resources)

    async with app.router.lifespan_context(app):
        assert closed == []
    assert closed == [True]


async def test_built_resources_probe_postgres_and_redis_and_close_cleanly() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@db.invalid/livecart"),
        redis_url=SecretStr("redis://redis.invalid:6379/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        comment_webhook_secret_simulator=TEST_COMMENT_WEBHOOK_SECRET,
        _env_file=None,
    )

    resources = build_resources(settings)

    assert [check.name for check in resources.readiness_checks] == ["postgres", "redis"]
    assert isinstance(resources.sellers, SqlSellerRepository)
    assert isinstance(resources.refresh_tokens, SqlRefreshTokenRepository)
    assert isinstance(resources.passwords, Argon2PasswordHasher)
    assert isinstance(resources.clock, SystemClock)
    assert isinstance(resources.live_sessions, SqlLiveSessionRepository)
    assert isinstance(resources.products, SqlProductStockRepository)
    assert isinstance(resources.idempotency, RedisIdempotencyStore)
    assert isinstance(resources.orders, SqlOrderRepository)
    assert isinstance(resources.rate_limiter, RedisRateLimiter)
    assert list(resources.platforms) == [Platform.SIMULATOR]
    assert isinstance(resources.platforms[Platform.SIMULATOR], SimulatorPlatformAdapter)
    assert isinstance(resources.comment_queue, RedisCommentQueue)
    await resources.aclose()  # nothing was connected: closing must not fail


def _startup_settings(**overrides: object) -> Settings:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@db.invalid/livecart"),
        redis_url=SecretStr("redis://redis.invalid:6379/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        comment_webhook_secret_simulator=TEST_COMMENT_WEBHOOK_SECRET,
        _env_file=None,
    )
    return settings.model_copy(update=overrides)


@pytest.mark.parametrize("stub", [Platform.FACEBOOK, Platform.INSTAGRAM, Platform.TIKTOK])
def test_the_api_refuses_to_start_with_a_platform_stub_enabled(stub: Platform) -> None:
    settings = _startup_settings(enabled_platforms=(Platform.SIMULATOR, stub))

    with pytest.raises(NotImplementedError, match=f"{stub} comment webhook is a documented stub"):
        build_resources(settings)


def test_the_api_refuses_to_start_without_the_webhook_secret_of_the_simulator() -> None:
    settings = _startup_settings(comment_webhook_secret_simulator=None)

    with pytest.raises(ValueError, match="COMMENT_WEBHOOK_SECRET_SIMULATOR must be set"):
        build_resources(settings)


async def test_unreachable_dependencies_answer_503() -> None:
    # Real engine and Redis client aimed at a closed port: no Docker needed.
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@127.0.0.1:1/livecart"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        comment_webhook_secret_simulator=TEST_COMMENT_WEBHOOK_SECRET,
        allowed_hosts=(TEST_HOST,),
        _env_file=None,
    )
    resources = build_resources(settings)
    app = create_app(settings, resources, readiness_timeout_s=0.5)

    try:
        async with open_client(app) as client:
            response = await client.get("/ready")
    finally:
        await resources.aclose()

    assert response.status_code == 503
    assert set(response.json()["checks"]) == {"postgres", "redis"}
    assert set(response.json()["checks"].values()) <= {"failed", "timeout"}
