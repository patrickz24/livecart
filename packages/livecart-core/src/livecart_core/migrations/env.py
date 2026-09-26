"""Alembic environment of livecart_core, loaded by ``livecart-migrate``.

The database URL comes from ``config.attributes["database_url"]`` (the
``MIGRATOR_URL`` setting), never from an ini file, so no credential sits in a
configuration file. Migrations run as ``livecart_migrator``, the schema owner.

``target_metadata`` is the schema of ``livecart_core.infrastructure.db.tables``,
which ``alembic revision --autogenerate`` compares with the database to draft
the next revision. The revisions themselves never import it.
"""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from livecart_core.infrastructure.db.tables import metadata

config = context.config

target_metadata = metadata


def _database_url() -> str:
    url = config.attributes.get("database_url")
    if not isinstance(url, str):
        raise RuntimeError("run migrations with livecart-migrate, which provides the database URL")
    return url


def run_migrations_offline() -> None:
    """Render the SQL instead of executing it (``livecart-migrate upgrade head --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(
        _database_url(),
        poolclass=pool.NullPool,
        connect_args={"server_settings": {"application_name": "livecart-migrate"}},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
