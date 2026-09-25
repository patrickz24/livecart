import asyncio
import time
from typing import Any

from livecart_api.resources import ReadinessCheck


async def test_health_answers_without_probing_any_dependency(
    app_factory: Any, serve: Any, fake_probe: Any
) -> None:
    probe = fake_probe()
    probe.available = False
    app = app_factory([ReadinessCheck("postgres", probe)])

    async with serve(app) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert probe.calls == 0


async def test_ready_returns_200_then_503_when_a_dependency_goes_down(
    app_factory: Any, serve: Any, fake_probe: Any
) -> None:
    postgres, redis = fake_probe(), fake_probe()
    app = app_factory([ReadinessCheck("postgres", postgres), ReadinessCheck("redis", redis)])

    async with serve(app) as client:
        first = await client.get("/ready")
        redis.available = False
        second = await client.get("/ready")

    assert first.status_code == 200
    assert first.json() == {"status": "ready", "checks": {"postgres": "ok", "redis": "ok"}}
    assert second.status_code == 503
    assert second.json() == {
        "status": "not_ready",
        "checks": {"postgres": "ok", "redis": "failed"},
    }
    assert (postgres.calls, redis.calls) == (2, 2)


async def test_ready_recovers_when_the_dependency_comes_back(
    app_factory: Any, serve: Any, fake_probe: Any
) -> None:
    postgres = fake_probe()
    postgres.available = False
    app = app_factory([ReadinessCheck("postgres", postgres)])

    async with serve(app) as client:
        down = await client.get("/ready")
        postgres.available = True
        up = await client.get("/ready")

    assert (down.status_code, up.status_code) == (503, 200)


async def test_a_hanging_dependency_times_out_with_503(app_factory: Any, serve: Any) -> None:
    async def hang() -> None:
        await asyncio.Event().wait()

    app = app_factory([ReadinessCheck("redis", hang)], timeout_s=0.1)

    async with serve(app) as client:
        started = time.perf_counter()
        response = await client.get("/ready")
        elapsed = time.perf_counter() - started

    assert response.status_code == 503
    assert response.json()["checks"] == {"redis": "timeout"}
    assert elapsed < 1.0


async def test_checks_run_in_parallel(app_factory: Any, serve: Any) -> None:
    # Each probe waits for the other one to start: run one after the other,
    # the first would wait forever and time out.
    postgres_started, redis_started = asyncio.Event(), asyncio.Event()

    async def postgres() -> None:
        postgres_started.set()
        await redis_started.wait()

    async def redis() -> None:
        redis_started.set()
        await postgres_started.wait()

    app = app_factory(
        [ReadinessCheck("postgres", postgres), ReadinessCheck("redis", redis)], timeout_s=0.5
    )

    async with serve(app) as client:
        response = await client.get("/ready")

    assert response.status_code == 200


async def test_failure_details_stay_out_of_the_response(app_factory: Any, serve: Any) -> None:
    async def leaky() -> None:
        raise RuntimeError("connection to db.internal:5432 failed for user livecart_app")

    app = app_factory([ReadinessCheck("postgres", leaky)])

    async with serve(app) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert "db.internal" not in response.text
    assert "livecart_app" not in response.text
