"""pytest fixtures for integration tests, registered for every member by the root conftest.py.

They target the compose PostgreSQL and Redis published on 127.0.0.1, through
the TEST_* variables. Only tests marked ``integration`` request them, so the
unit suite never needs Docker. Every database fixture goes through
``load_integration_settings``, which refuses a database not named ``*_test``.
"""

from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.infrastructure.db.engine import create_db_engine
from livecart_core.infrastructure.redis_client import create_redis_client
from livecart_core.testing.database import (
    IntegrationSettings,
    IntegrationSettingsError,
    UnsafeTestDatabaseError,
    load_integration_settings,
    unreachable_servers,
)


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
async def redis_client(integration_settings: IntegrationSettings) -> AsyncIterator[Redis]:
    client = create_redis_client(integration_settings.redis_url)
    try:
        yield client
    finally:
        await client.aclose()
