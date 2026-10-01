"""Units of work on PostgreSQL: one transaction per repository method, errors mapped onto the ports.

The ports promise that a repository method is atomic and that a failure worth
retrying raises ``ServiceUnavailable``. ``transaction`` gives both: the block
runs in one READ COMMITTED transaction (the engine's isolation level), which
commits when the block ends and rolls back when anything escapes it, and the
failures that another attempt may not meet come out as ``ServiceUnavailable``:
a lock or statement timeout, a deadlock or serialization failure, a lost or
refused connection, a pool with no free connection in time. Every other error
(a violated constraint, a programming error) keeps its type.

SQLAlchemy wraps the driver's errors in its own ``DBAPIError``, except those
asyncpg raises while it opens a connection: a server that is starting up or
shutting down refuses it with asyncpg's own ``PostgresError``
(``CannotConnectNowError``, 57P03, during a restart), which reaches
``transaction`` unwrapped. Its SQLSTATE decides the same way.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy import exc
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from livecart_core.application.ports import ServiceUnavailable

# SQLSTATE codes of transient failures: the exact codes, then whole classes.
TRANSIENT_SQLSTATES: Final = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "55P03",  # lock_not_available: lock_timeout
        "57014",  # query_canceled: statement_timeout
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
    }
)
TRANSIENT_SQLSTATE_CLASSES: Final = frozenset(
    {
        "08",  # connection_exception
        "53",  # insufficient_resources, too_many_connections among them
    }
)


def sqlstate(error: exc.DBAPIError) -> str | None:
    """The SQLSTATE code PostgreSQL reported, when it reported one."""
    return _driver_sqlstate(error.orig)


def _driver_sqlstate(error: BaseException | None) -> str | None:
    """The SQLSTATE of a driver's error, asyncpg's own or its adapter's, named ``sqlstate``."""
    # asyncpg ships no type information: its errors are read by attribute, as
    # violated_constraint reads them.
    code = getattr(error, "sqlstate", None)
    return code if isinstance(code, str) else None


def _is_transient_code(code: str | None) -> bool:
    return code is not None and (
        code in TRANSIENT_SQLSTATES or code[:2] in TRANSIENT_SQLSTATE_CLASSES
    )


def violated_constraint(error: exc.IntegrityError) -> str | None:
    """The name of the constraint or unique index an ``IntegrityError`` violated."""
    # SQLAlchemy's asyncpg adapter raises its DBAPI error from asyncpg's own,
    # which carries the constraint name.
    cause = error.orig.__cause__ if error.orig is not None else None
    name = getattr(cause, "constraint_name", None)
    return name if isinstance(name, str) else None


def is_transient(error: BaseException) -> bool:
    """Whether a new attempt of the same unit may succeed."""
    if isinstance(error, exc.TimeoutError | OSError):
        # No pooled connection freed in time, or no connection could be opened.
        return True
    if not isinstance(error, exc.DBAPIError):
        # asyncpg's own error, unwrapped, when the server refused a new
        # connection (see the module docstring).
        return _is_transient_code(_driver_sqlstate(error))
    if error.connection_invalidated or isinstance(error, exc.InterfaceError):
        return True
    return _is_transient_code(sqlstate(error))


@asynccontextmanager
async def transaction(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """One unit of work, committed at the end of the block; see the module docstring."""
    try:
        async with engine.begin() as connection:
            yield connection
    except Exception as error:
        # Every error but a transient one goes on with its own type, unchanged.
        if is_transient(error):
            raise ServiceUnavailable(f"database unavailable: {type(error).__name__}") from error
        raise
