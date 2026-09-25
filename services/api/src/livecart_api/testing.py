"""Test doubles of the API's unit tests: the app is built around fakes, never PostgreSQL or Redis.

Only test code imports this module, which needs the dev dependency group
(httpx2); the runtime never does. It sits in the package, like
``livecart_core.testing``, because under pytest's importlib mode the modules of
a tests folder cannot import each other: the tests import these helpers, with
their types, from an installed module instead of receiving untyped fixtures.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx2
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
    """The app around ``checks``, with resources that hold nothing to close."""
    resources = Resources(readiness_checks=checks, aclose=_noop)
    return create_app(make_settings(app_env), resources, readiness_timeout_s=timeout_s)


@asynccontextmanager
async def open_client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """An in-process HTTP client of ``app`` (the lifespan is tested separately)."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
