"""Test doubles of the API's unit tests: the app is built around fakes, never PostgreSQL or Redis.

Only test code imports this module, which needs the dev dependency group
(httpx2); the runtime never does. It sits in the package, like
``livecart_core.testing``, because under pytest's importlib mode the modules of
a tests folder cannot import each other: the tests import these helpers, with
their types, from an installed module instead of receiving untyped fixtures.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import httpx2
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_api.access_tokens import AccessTokens
from livecart_api.app import create_app
from livecart_api.resources import ReadinessCheck, Resources
from livecart_core.application.ports import Clock, IdempotencyStore, PasswordHasher
from livecart_core.infrastructure.db.live_sessions import SqlLiveSessionRepository
from livecart_core.infrastructure.db.products import SqlProductStockRepository
from livecart_core.infrastructure.db.refresh_tokens import SqlRefreshTokenRepository
from livecart_core.infrastructure.db.sellers import SqlSellerRepository
from livecart_core.infrastructure.passwords import Argon2PasswordHasher
from livecart_core.settings import AppEnv, Settings
from livecart_core.testing.backends import CURRENCY
from livecart_core.testing.fakes import (
    FakeClock,
    FakePasswordHasher,
    InMemoryDatabase,
    InMemoryIdempotencyStore,
)
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


def memory_resources(
    database: InMemoryDatabase,
    *,
    checks: Sequence[ReadinessCheck] = (),
    passwords: PasswordHasher | None = None,
    clock: Clock | None = None,
    idempotency: IdempotencyStore | None = None,
) -> Resources:
    """Resources over the in-memory repositories of ``database``, with nothing to close.

    A test that needs one repository to behave otherwise ``replace``s it.
    """
    return Resources(
        readiness_checks=checks,
        aclose=_noop,
        sellers=database.sellers,
        refresh_tokens=database.refresh_tokens,
        passwords=passwords if passwords is not None else FakePasswordHasher(),
        clock=clock if clock is not None else current_time_clock(),
        live_sessions=database.live_sessions,
        products=database.products,
        idempotency=idempotency if idempotency is not None else InMemoryIdempotencyStore(),
    )


def postgres_resources(
    engine: AsyncEngine, *, clock: Clock, idempotency: IdempotencyStore | None = None
) -> Resources:
    """Resources over the SQL adapters on ``engine`` (livecart_app on livecart_test) and argon2id.

    The integration tests own the engine, and the Redis client of the store
    they may pass: nothing is closed with the app.
    """
    return Resources(
        readiness_checks=(),
        aclose=_noop,
        sellers=SqlSellerRepository(engine),
        refresh_tokens=SqlRefreshTokenRepository(engine),
        passwords=Argon2PasswordHasher(),
        clock=clock,
        live_sessions=SqlLiveSessionRepository(engine),
        products=SqlProductStockRepository(engine, currency=CURRENCY),
        idempotency=idempotency if idempotency is not None else InMemoryIdempotencyStore(),
    )


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
    resources = memory_resources(
        database if database is not None else InMemoryDatabase(),
        checks=checks,
        passwords=passwords,
        clock=clock,
    )
    return create_app(make_settings(app_env), resources, readiness_timeout_s=timeout_s)


def seller_headers(app: FastAPI, seller_id: UUID) -> dict[str, str]:
    """``Authorization: Bearer`` with a fresh access token of ``seller_id``, issued by ``app``."""
    tokens = app.state.access_tokens
    if not isinstance(tokens, AccessTokens):
        raise RuntimeError("the app was built without an access token issuer")
    return {"Authorization": f"Bearer {tokens.issue(seller_id).value}"}


@asynccontextmanager
async def open_client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """An in-process HTTP client of ``app`` (the lifespan is tested separately)."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=TEST_ORIGIN) as client:
        yield client


@dataclass(frozen=True, slots=True)
class SellerRoute:
    """A seller route that names a session, and a product for some, with a body it accepts."""

    method: str
    path: str
    body: dict[str, object] | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def url(self, session_id: UUID, product_id: UUID) -> str:
        return self.path.format(session_id=session_id, product_id=product_id)


SESSION_PATH = "/api/v1/live-sessions/{session_id}"
PRODUCT_PATH = "/api/v1/live-sessions/{session_id}/products/{product_id}"
# Every seller route that names an object, with a body that the owner's request would get
# past validation with: another seller must get the 404 of an unknown id from each one.
SELLER_OBJECT_ROUTES: tuple[SellerRoute, ...] = (
    SellerRoute("GET", SESSION_PATH),
    SellerRoute("PATCH", SESSION_PATH, {"title": "Taken over"}),
    SellerRoute("POST", f"{SESSION_PATH}/start"),
    SellerRoute("POST", f"{SESSION_PATH}/end"),
    SellerRoute(
        "POST",
        f"{SESSION_PATH}/products",
        {"code": "Z9", "name": "Intruder", "price_minor": 1, "stock_total": 1},
    ),
    SellerRoute("GET", f"{SESSION_PATH}/products"),
    SellerRoute("GET", PRODUCT_PATH),
    SellerRoute("PATCH", PRODUCT_PATH, {"name": "Taken over"}),
    SellerRoute(
        "POST", f"{PRODUCT_PATH}/restock", {"delta": 1}, {"Idempotency-Key": "isolation-check"}
    ),
)
# The seller routes that name no object: they read or create the caller's own rows only.
SELLER_OWN_ROUTES = frozenset(
    {
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/live-sessions"),
        ("POST", "/api/v1/live-sessions"),
    }
)


def seller_routes(app: FastAPI) -> set[tuple[str, str]]:
    """(method, path) of every route of ``app`` that requires a seller's access token."""
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method, operation in operations.items()
        if {"SellerAccessToken": []} in operation.get("security", [])
    }
