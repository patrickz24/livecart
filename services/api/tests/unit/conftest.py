"""Unit-test helpers: the app is built around fakes, never around PostgreSQL or Redis."""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from livecart_api.app import create_app
from livecart_api.resources import ReadinessCheck, Resources
from livecart_core.settings import AppEnv, Settings


class FakeProbe:
    """A dependency whose availability a test switches at will."""

    def __init__(self) -> None:
        self.available = True
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if not self.available:
            raise ConnectionRefusedError("dependency down")


def make_settings(app_env: AppEnv = AppEnv.DEVELOPMENT) -> Settings:
    return Settings(
        app_env=app_env,
        database_url=SecretStr("postgresql+asyncpg://livecart_app:unit@db.invalid/livecart"),
        redis_url=SecretStr("redis://:unit@redis.invalid:6379/0"),
        _env_file=None,
    )


async def _noop() -> None:
    return None


def make_app(
    checks: Sequence[ReadinessCheck] = (),
    *,
    app_env: AppEnv = AppEnv.DEVELOPMENT,
    timeout_s: float = 1.0,
) -> FastAPI:
    resources = Resources(readiness_checks=checks, aclose=_noop)
    return create_app(make_settings(app_env), resources, readiness_timeout_s=timeout_s)


AppFactory = Callable[..., FastAPI]
Serve = Callable[[FastAPI], AbstractAsyncContextManager[httpx2.AsyncClient]]


@pytest.fixture
def app_factory() -> AppFactory:
    return make_app


@pytest.fixture
def serve() -> Serve:
    """Open an in-process HTTP client on an app (the lifespan is tested separately)."""

    @asynccontextmanager
    async def open_client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    return open_client


@pytest.fixture
def fake_probe() -> Callable[[], FakeProbe]:
    return FakeProbe
