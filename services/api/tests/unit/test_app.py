import uuid
from typing import Any

import pytest
import structlog
from pydantic import SecretStr

from livecart_api.app import create_app
from livecart_api.resources import ReadinessCheck, Resources, build_resources
from livecart_core.settings import AppEnv, Settings


async def test_a_request_id_is_generated_and_returned(app_factory: Any, serve: Any) -> None:
    async with serve(app_factory()) as client:
        response = await client.get("/health")

    assert uuid.UUID(response.headers["X-Request-ID"])


async def test_a_valid_incoming_request_id_is_kept(app_factory: Any, serve: Any) -> None:
    incoming = uuid.uuid4().hex

    async with serve(app_factory()) as client:
        response = await client.get("/health", headers={"X-Request-ID": incoming})

    assert response.headers["X-Request-ID"] == incoming


async def test_an_invalid_incoming_request_id_is_replaced(app_factory: Any, serve: Any) -> None:
    async with serve(app_factory()) as client:
        response = await client.get("/health", headers={"X-Request-ID": "forged id"})

    assert response.headers["X-Request-ID"] != "forged id"
    assert uuid.UUID(response.headers["X-Request-ID"])


async def test_the_request_id_is_bound_to_the_log_context(app_factory: Any, serve: Any) -> None:
    seen: list[object] = []

    async def probe() -> None:
        seen.append(structlog.contextvars.get_contextvars().get("request_id"))

    async with serve(app_factory([ReadinessCheck("postgres", probe)])) as client:
        response = await client.get("/ready")

    assert seen == [response.headers["X-Request-ID"]]
    assert "request_id" not in structlog.contextvars.get_contextvars()


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
async def test_docs_are_served_in_development(app_factory: Any, serve: Any, path: str) -> None:
    async with serve(app_factory(app_env=AppEnv.DEVELOPMENT)) as client:
        response = await client.get(path)

    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_docs_are_disabled_in_production(app_factory: Any, serve: Any, path: str) -> None:
    async with serve(app_factory(app_env=AppEnv.PRODUCTION)) as client:
        response = await client.get(path)

    assert response.status_code == 404


def test_openapi_documents_both_probes(app_factory: Any) -> None:
    schema = app_factory(app_env=AppEnv.PRODUCTION).openapi()

    health, ready = schema["paths"]["/health"]["get"], schema["paths"]["/ready"]["get"]
    assert health["operationId"] == "get_health"
    assert ready["operationId"] == "get_readiness"
    assert set(ready["responses"]) == {"200", "503"}
    assert {"HealthStatus", "ReadinessStatus"} <= set(schema["components"]["schemas"])


async def test_shutdown_closes_the_injected_resources(app_factory: Any) -> None:
    closed: list[bool] = []

    async def aclose() -> None:
        closed.append(True)

    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@db.invalid/livecart"),
        redis_url=SecretStr("redis://redis.invalid:6379/0"),
        _env_file=None,
    )
    app = create_app(settings, Resources(readiness_checks=(), aclose=aclose))

    async with app.router.lifespan_context(app):
        assert closed == []
    assert closed == [True]


async def test_built_resources_probe_postgres_and_redis_and_close_cleanly() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@db.invalid/livecart"),
        redis_url=SecretStr("redis://redis.invalid:6379/0"),
        _env_file=None,
    )

    resources = build_resources(settings)

    assert [check.name for check in resources.readiness_checks] == ["postgres", "redis"]
    await resources.aclose()  # nothing was connected: closing must not fail


async def test_unreachable_dependencies_answer_503(serve: Any) -> None:
    # Real engine and Redis client aimed at a closed port: no Docker needed.
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@127.0.0.1:1/livecart"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        _env_file=None,
    )
    resources = build_resources(settings)
    app = create_app(settings, resources, readiness_timeout_s=0.5)

    try:
        async with serve(app) as client:
            response = await client.get("/ready")
    finally:
        await resources.aclose()

    assert response.status_code == 503
    assert set(response.json()["checks"]) == {"postgres", "redis"}
    assert set(response.json()["checks"].values()) <= {"failed", "timeout"}
