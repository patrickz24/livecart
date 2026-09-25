"""livecart-migrate against the compose livecart_test database, as livecart_migrator.

These tests are synchronous on purpose: Alembic's env.py runs its own event loop.
"""

import asyncio

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from livecart_core.migrate import execute, parse_args
from livecart_core.testing.database import IntegrationSettings

pytestmark = pytest.mark.integration


def migrate(url: SecretStr, *argv: str) -> None:
    cli, options = parse_args(argv)
    execute(cli, options, url)


def recorded_versions(url: SecretStr) -> list[str] | None:
    """Rows of alembic_version, or None when the table does not exist."""

    async def fetch() -> list[str] | None:
        engine = create_async_engine(url.get_secret_value())
        try:
            async with engine.connect() as connection:
                exists = (
                    await connection.execute(text("SELECT to_regclass('public.alembic_version')"))
                ).scalar_one()
                if exists is None:
                    return None
                rows = await connection.execute(text("SELECT version_num FROM alembic_version"))
                return [row[0] for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(fetch())


def test_upgrade_then_downgrade_the_baseline(integration_settings: IntegrationSettings) -> None:
    url = integration_settings.migrator_url
    migrate(url, "downgrade", "base")

    migrate(url, "upgrade", "head")
    assert recorded_versions(url) == ["0001"]

    migrate(url, "downgrade", "base")
    assert recorded_versions(url) in (None, [])

    migrate(url, "upgrade", "head")
    assert recorded_versions(url) == ["0001"]
