"""The schema of the integration tests: migrated once per session, emptied before each test.

The fixtures of ``fixtures.py`` call these helpers as ``livecart_migrator``,
the schema owner: the plan's fixtures truncate as the migrator, while the code
under test runs as ``livecart_app``, which may never delete a row.
"""

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.infrastructure.db.tables import metadata
from livecart_core.migrate import execute, parse_args


def migrate_to_head(migrator_url: SecretStr) -> None:
    """``livecart-migrate upgrade head``.

    Synchronous on purpose: the Alembic environment runs its own event loop,
    so call it from a synchronous fixture or test, never from a coroutine.
    """
    cli, options = parse_args(["upgrade", "head"])
    execute(cli, options, migrator_url)


def truncate_statement() -> str:
    """One ``TRUNCATE`` of every table of the schema, whatever their foreign keys."""
    names = ", ".join(table.name for table in metadata.sorted_tables)
    return f"TRUNCATE TABLE {names}"


async def truncate_all(migrator_engine: AsyncEngine) -> None:
    """Delete every row of every table; ``migrator_engine`` must connect as livecart_migrator."""
    async with migrator_engine.begin() as connection:
        await connection.execute(text(truncate_statement()))
