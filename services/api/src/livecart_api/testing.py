"""Test doubles of the API's unit tests: the app is built around fakes, never PostgreSQL or Redis.

Only test code imports this module, which needs the dev dependency group
(httpx2); the runtime never does. It sits in the package, like
``livecart_core.testing``, because under pytest's importlib mode the modules of
a tests folder cannot import each other: the tests import these helpers, with
their types, from an installed module instead of receiving untyped fixtures.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx2
from fastapi import FastAPI
from pydantic import SecretStr

from livecart_api.app import create_app
from livecart_api.resources import ReadinessCheck, Resources
from livecart_core.application.ports import Clock, PasswordHasher
from livecart_core.settings import AppEnv, Settings
from livecart_core.testing.fakes import FakeClock, FakePasswordHasher, InMemoryDatabase
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY

# The origin of open_client's requests, as a browser would send it in Origin.
TEST_ORIGIN = "http://test"


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
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        _env_file=None,
    )


def current_time_clock() -> FakeClock:
    """A fake clock that starts now: PyJWT checks ``exp`` against the system clock."""
    return FakeClock(datetime.now(UTC))


async def _noop() -> None:
    return None


def make_app(
    checks: Sequence[ReadinessCheck] = (),
    *,
    app_env: AppEnv = AppEnv.DEVELOPMENT,
    timeout_s: float = 1.0,
    database: InMemoryDatabase | None = None,
    passwords: PasswordHasher | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """The app around ``checks`` and in-memory repositories, with nothing to close."""
    database = database if database is not None else InMemoryDatabase()
    resources = Resources(
        readiness_checks=checks,
        aclose=_noop,
        sellers=database.sellers,
        refresh_tokens=database.refresh_tokens,
        passwords=passwords if passwords is not None else FakePasswordHasher(),
        clock=clock if clock is not None else current_time_clock(),
    )
    return create_app(make_settings(app_env), resources, readiness_timeout_s=timeout_s)


@asynccontextmanager
async def open_client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """An in-process HTTP client of ``app`` (the lifespan is tested separately)."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=TEST_ORIGIN) as client:
        yield client
