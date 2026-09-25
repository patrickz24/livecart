"""Connection settings of the integration tests and the guard that protects real data.

Integration tests truncate and rewrite tables. They therefore refuse to run
against any database whose name does not end in ``_test``, so a typo in
TEST_DATABASE_URL can never wipe the development database. Likewise, they only
use Redis database 15, which the API and the worker (database 0) never touch,
and each test writes under a key prefix of its own (``redis_keys.py``).
"""

import socket
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

TEST_DATABASE_SUFFIX = "_test"
TEST_REDIS_DATABASE = 15


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when a test fixture is pointed at real data: a PostgreSQL database
    not named ``*_test``, or a Redis database other than 15."""


class IntegrationSettingsError(RuntimeError):
    """Raised when the TEST_* variables are missing or invalid."""


def ensure_test_database(url: SecretStr, *, variable: str) -> str:
    """Return the database name of ``url`` if it ends in ``_test``, else raise.

    The error names the variable and the database, never the URL (it holds a password).
    """
    database = make_url(url.get_secret_value()).database
    if not database or not database.endswith(TEST_DATABASE_SUFFIX):
        raise UnsafeTestDatabaseError(
            f"{variable} targets database {database!r}; test fixtures only run against a "
            f"database whose name ends in {TEST_DATABASE_SUFFIX!r}, such as 'livecart_test'"
        )
    return database


def redis_database(url: SecretStr) -> int | None:
    """The Redis database that ``url`` selects, or None when it is not a number.

    Same precedence as redis-py's ``Redis.from_url``: a ``db`` query parameter
    wins over the path (``/15``), and a URL with neither selects database 0.
    """
    parts = urlsplit(url.get_secret_value())
    query = parse_qs(parts.query).get("db")
    if query:
        raw = unquote(query[0])
    elif parts.scheme == "unix":
        raw = ""  # the path of a unix:// URL is the socket, not a database
    else:
        raw = unquote(parts.path).replace("/", "")
    if raw == "":
        return 0
    return int(raw) if raw.isascii() and raw.isdecimal() else None


def ensure_test_redis_database(url: SecretStr, *, variable: str) -> int:
    """Return the Redis database of ``url`` if it is 15, else raise.

    The error names the variable and the database, never the URL (it holds a password).
    """
    database = redis_database(url)
    if database != TEST_REDIS_DATABASE:
        raise UnsafeTestDatabaseError(
            f"{variable} selects Redis database {database!r}; test fixtures only run against "
            f"database {TEST_REDIS_DATABASE}, as in redis://:<password>@127.0.0.1:56379/15"
        )
    return database


class IntegrationSettings(BaseSettings):
    """TEST_DATABASE_URL, TEST_MIGRATOR_URL and TEST_REDIS_URL, from the environment or .env."""

    model_config = SettingsConfigDict(
        env_prefix="TEST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    database_url: SecretStr
    migrator_url: SecretStr
    redis_url: SecretStr


def load_integration_settings(env_file: Path | None = Path(".env")) -> IntegrationSettings:
    """Load and guard the integration settings; raise a readable error otherwise."""
    try:
        settings = IntegrationSettings(_env_file=env_file)
    except ValidationError as exc:
        raise IntegrationSettingsError(
            "integration tests need TEST_DATABASE_URL, TEST_MIGRATOR_URL and TEST_REDIS_URL "
            "(copy .env.example to .env, then run `docker compose up -d --wait postgres redis`), "
            f"or deselect them with -m 'not integration'.\n{exc}"
        ) from None
    ensure_test_database(settings.database_url, variable="TEST_DATABASE_URL")
    ensure_test_database(settings.migrator_url, variable="TEST_MIGRATOR_URL")
    ensure_test_redis_database(settings.redis_url, variable="TEST_REDIS_URL")
    return settings


def unreachable_servers(settings: IntegrationSettings, *, timeout_s: float = 2.0) -> list[str]:
    """Return ``host:port`` for each test server that does not accept a TCP connection.

    Checked once per session, so a forgotten ``docker compose up`` fails fast with
    one clear message instead of a connection timeout in every test.
    """
    targets: dict[tuple[str, int], None] = {}
    for url in (settings.database_url, settings.migrator_url):
        parsed = make_url(url.get_secret_value())
        targets[(parsed.host or "localhost", parsed.port or 5432)] = None
    redis = urlsplit(settings.redis_url.get_secret_value())
    targets[(redis.hostname or "localhost", redis.port or 6379)] = None

    down = []
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                pass
        except OSError:
            down.append(f"{host}:{port}")
    return down
