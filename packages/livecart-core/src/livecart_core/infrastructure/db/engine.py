"""Async engine factory: each process builds exactly one engine at startup."""

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Server-side guards applied to every connection, in milliseconds.
STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 2000


def create_db_engine(
    url: SecretStr,
    *,
    application_name: str,
    pool_size: int,
    max_overflow: int,
    pool_timeout_s: float = 10.0,
) -> AsyncEngine:
    """Create the process-wide engine; no connection is opened until first use.

    ``hide_parameters`` keeps bound values (which may be personal data or
    tokens) out of SQLAlchemy exception messages and therefore out of logs.
    """
    return create_async_engine(
        url.get_secret_value(),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout_s,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "application_name": application_name,
                "statement_timeout": str(STATEMENT_TIMEOUT_MS),
                "lock_timeout": str(LOCK_TIMEOUT_MS),
            }
        },
    )


async def ping_database(engine: AsyncEngine) -> None:
    """Run ``SELECT 1``; raise if PostgreSQL cannot be reached."""
    async with engine.connect() as connection:
        await connection.execute(select(1))
