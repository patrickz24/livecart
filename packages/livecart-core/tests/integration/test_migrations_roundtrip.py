"""livecart-migrate against the compose livecart_test database, as livecart_migrator.

These tests are synchronous on purpose: Alembic's env.py runs its own event loop.
"""

import asyncio

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from livecart_core.infrastructure.db.tables import metadata
from livecart_core.migrate import execute, parse_args
from livecart_core.testing.database import IntegrationSettings

pytestmark = pytest.mark.integration

HEAD = "0003"


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


def schema_tables(url: SecretStr) -> set[str]:
    """The tables of the public schema, the migration history left aside."""

    async def fetch() -> set[str]:
        engine = create_async_engine(url.get_secret_value())
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                        " AND tablename <> 'alembic_version'"
                    )
                )
                return {row[0] for row in rows}
        finally:
            await engine.dispose()

    return asyncio.run(fetch())


def test_upgrade_downgrade_then_upgrade_again(integration_settings: IntegrationSettings) -> None:
    url = integration_settings.migrator_url
    migrate(url, "downgrade", "base")

    migrate(url, "upgrade", "head")
    assert recorded_versions(url) == [HEAD]
    assert schema_tables(url) == set(metadata.tables)

    migrate(url, "downgrade", "base")
    assert recorded_versions(url) in (None, [])
    assert schema_tables(url) == set()

    migrate(url, "upgrade", "head")
    assert recorded_versions(url) == [HEAD]
    assert schema_tables(url) == set(metadata.tables)


def test_each_revision_steps_up_and_down_on_its_own(
    integration_settings: IntegrationSettings,
) -> None:
    url = integration_settings.migrator_url
    migrate(url, "downgrade", "base")

    migrate(url, "upgrade", "0001")
    assert (recorded_versions(url), schema_tables(url)) == (["0001"], set())

    migrate(url, "upgrade", "+1")
    assert (recorded_versions(url), schema_tables(url)) == (["0002"], set(metadata.tables))

    migrate(url, "upgrade", "+1")
    assert (recorded_versions(url), schema_tables(url)) == ([HEAD], set(metadata.tables))

    migrate(url, "downgrade", "-1")
    assert (recorded_versions(url), schema_tables(url)) == (["0002"], set(metadata.tables))

    migrate(url, "downgrade", "-1")
    assert (recorded_versions(url), schema_tables(url)) == (["0001"], set())

    migrate(url, "upgrade", "head")
    assert recorded_versions(url) == [HEAD]
