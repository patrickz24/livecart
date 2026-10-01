"""The mapping of database failures onto the ports, without a database."""

import importlib
from typing import Final

import pytest
from sqlalchemy import exc
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from livecart_core.application.ports import ServiceUnavailable
from livecart_core.infrastructure.db.units import (
    is_transient,
    sqlstate,
    transaction,
    violated_constraint,
)


class _DriverError(Exception):
    """What SQLAlchemy's asyncpg adapter raises: an error carrying its SQLSTATE."""

    def __init__(self, code: str | None) -> None:
        super().__init__("driver error")
        self.sqlstate = code


class _AsyncpgError(Exception):
    """asyncpg's own error, the cause of the adapter's, with the violated constraint."""

    def __init__(self, constraint_name: str | None) -> None:
        super().__init__("asyncpg error")
        self.constraint_name = constraint_name


def _dbapi_error(code: str | None, *, invalidated: bool = False) -> exc.DBAPIError:
    return exc.DBAPIError("SELECT 1", None, _DriverError(code), connection_invalidated=invalidated)


@pytest.mark.parametrize(
    "code",
    ["40001", "40P01", "55P03", "57014", "57P01", "57P02", "57P03", "08006", "08001", "53300"],
)
def test_timeouts_conflicts_and_lost_connections_are_transient(code: str) -> None:
    assert is_transient(_dbapi_error(code))


@pytest.mark.parametrize("code", ["23505", "23514", "42501", "42P01", "22003", None])
def test_violations_and_programming_errors_are_not(code: str | None) -> None:
    assert not is_transient(_dbapi_error(code))


def test_an_invalidated_connection_or_a_closed_interface_is_transient() -> None:
    assert is_transient(_dbapi_error(None, invalidated=True))
    assert is_transient(exc.InterfaceError("SELECT 1", None, _DriverError(None)))


def test_a_pool_timeout_or_a_refused_connection_is_transient() -> None:
    assert is_transient(exc.TimeoutError("QueuePool limit reached"))
    assert is_transient(ConnectionRefusedError("connection refused"))
    assert not is_transient(ValueError("a bug"))


def test_the_sqlstate_is_read_from_the_driver_error() -> None:
    assert sqlstate(_dbapi_error("23505")) == "23505"
    assert sqlstate(exc.DBAPIError("SELECT 1", None, ValueError("no code"))) is None


def test_the_violated_constraint_is_read_from_the_asyncpg_cause() -> None:
    driver = _DriverError("23505")
    driver.__cause__ = _AsyncpgError("uq_orders_reference")

    assert violated_constraint(exc.IntegrityError("INSERT", None, driver)) == "uq_orders_reference"
    assert violated_constraint(exc.IntegrityError("INSERT", None, _DriverError("23505"))) is None


# --- Connections the server refuses ------------------------------------------------------

# asyncpg ships no type information (no py.typed): its exceptions are looked up
# by name, so that mypy --strict needs no ignore for them.
_ASYNCPG_ERRORS: Final = importlib.import_module("asyncpg.exceptions")


def _refusing_engine(error: Exception) -> AsyncEngine:
    """An engine whose pool's connection creator raises ``error``, as asyncpg's connect does."""

    async def refuse() -> object:
        raise error

    return create_async_engine("postgresql+asyncpg://", async_creator=refuse)


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("AdminShutdownError", "57P01"),
        ("CrashShutdownError", "57P02"),
        ("CannotConnectNowError", "57P03"),
        ("TooManyConnectionsError", "53300"),
    ],
)
async def test_a_connection_refused_while_the_server_restarts_is_unavailable(
    name: str, code: str
) -> None:
    # SQLAlchemy does not wrap what asyncpg raises while it connects: the
    # unit gets asyncpg's own PostgresError, which must not escape as a 500.
    error_type = getattr(_ASYNCPG_ERRORS, name)
    engine = _refusing_engine(error_type("the database system is starting up"))
    try:
        with pytest.raises(ServiceUnavailable) as raised:
            async with transaction(engine):
                pass
    finally:
        await engine.dispose()

    cause = raised.value.__cause__
    assert isinstance(cause, error_type)
    assert cause.sqlstate == code


async def test_a_connection_refused_for_good_keeps_the_drivers_error() -> None:
    error_type = _ASYNCPG_ERRORS.InvalidPasswordError
    engine = _refusing_engine(error_type("password authentication failed"))
    try:
        with pytest.raises(error_type):
            async with transaction(engine):
                pass
    finally:
        await engine.dispose()
