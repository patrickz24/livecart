"""Async engine factory: each process builds exactly one engine at startup.

Every connection runs in READ COMMITTED, with a statement timeout, a lock
timeout and an idle-in-transaction timeout set on the server side. READ
COMMITTED is what the stock reservation relies on: a blocked ``UPDATE``
re-evaluates its ``WHERE`` on the row version that the other transaction
committed, so a reservation never oversells and never needs a retry, where
REPEATABLE READ or SERIALIZABLE would fail it with a serialization error
instead. It is PostgreSQL's default too, but a server or database setting
could change the default; the engine does not depend on it.

The pools follow the plan's connection budget (97 connections available):

- the API holds 5 connections plus 10 on overflow;
- the worker holds one connection per comment handler (``WORKER_CONCURRENCY``,
  16) and one per background loop that uses the database (4: the consumer's
  reclaim and its dead letters, the expiry sweep, and from milestones 5 and 6
  the payment registry and the shipments), so 20, plus 2 on overflow: 22 at most, which
  ``WORKER_DB_MAX_CONNECTIONS`` caps at startup (``worker_pool``).

That is 37 connections for one API and one worker, 81 with three workers.
"""

from dataclasses import dataclass
from typing import Final

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Server-side guards applied to every connection, in milliseconds.
STATEMENT_TIMEOUT_MS: Final = 5000
LOCK_TIMEOUT_MS: Final = 2000
# A transaction a bug leaves open between two statements (an await on something
# else, a task stuck mid-unit) would hold its row locks until the process ends,
# failing every unit on those rows after lock_timeout. The server ends it after
# 30 s, the deadline of a whole comment and well under its 120 s reclaim; a unit
# is a few short statements, never idle that long.
IDLE_IN_TRANSACTION_TIMEOUT_MS: Final = 30_000
ISOLATION_LEVEL: Final = "READ COMMITTED"
# How long a caller waits for a free pooled connection before giving up.
POOL_TIMEOUT_S: Final = 10.0


@dataclass(frozen=True, slots=True)
class PoolSize:
    """The connections a process keeps open, and how many more it may open under load."""

    size: int
    max_overflow: int

    @property
    def max_connections(self) -> int:
        return self.size + self.max_overflow


# The worker's background loops that use the database, each sequential and on
# one connection at a time, and the overflow above them.
WORKER_DB_LOOPS: Final = 4
WORKER_POOL_OVERFLOW: Final = 2


def worker_pool(concurrency: int) -> PoolSize:
    """The worker's pool: a connection per comment handler and per database loop, plus overflow."""
    if concurrency < 1:
        raise ValueError(f"a worker runs at least one comment handler, not {concurrency}")
    return PoolSize(size=concurrency + WORKER_DB_LOOPS, max_overflow=WORKER_POOL_OVERFLOW)


API_POOL: Final = PoolSize(size=5, max_overflow=10)
# With the default WORKER_CONCURRENCY of 16: 20 connections, 22 at most.
WORKER_POOL: Final = worker_pool(16)


def create_db_engine(
    url: SecretStr,
    *,
    application_name: str,
    pool_size: int,
    max_overflow: int,
    pool_timeout_s: float = POOL_TIMEOUT_S,
) -> AsyncEngine:
    """Create the process-wide engine; no connection is opened until first use.

    ``pool_pre_ping`` checks a pooled connection before handing it out, so a
    restarted PostgreSQL costs a reconnection rather than a failed request.
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
        isolation_level=ISOLATION_LEVEL,
        connect_args={
            "server_settings": {
                "application_name": application_name,
                "statement_timeout": str(STATEMENT_TIMEOUT_MS),
                "lock_timeout": str(LOCK_TIMEOUT_MS),
                "idle_in_transaction_session_timeout": str(IDLE_IN_TRANSACTION_TIMEOUT_MS),
            }
        },
    )


async def ping_database(engine: AsyncEngine) -> None:
    """Run ``SELECT 1``; raise if PostgreSQL cannot be reached."""
    async with engine.connect() as connection:
        await connection.execute(select(1))
