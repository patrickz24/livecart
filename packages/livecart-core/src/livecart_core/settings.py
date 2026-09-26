"""Typed configuration, validated once when a process starts.

Values come from environment variables first, then from a ``.env`` file in the
working directory: host-run commands start from the repository root, and
containers receive the same variables from compose. Every secret, including
connection URLs that embed a password, is a ``SecretStr``: it never shows up in a
repr, a log line or a validation error.
"""

import re
from datetime import timedelta
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from livecart_core.application.dto import Platform
from livecart_core.domain.quantity import QUANTITY_CEILING
from livecart_core.healthcheck import DEFAULT_HEARTBEAT_FILE
from livecart_core.infrastructure.db.engine import (
    WORKER_DB_LOOPS,
    WORKER_POOL_OVERFLOW,
    PoolSize,
    worker_pool,
)
from livecart_core.order_tokens import MIN_KEY_BYTES

# Marker of the development placeholders shipped in .env.example.
PLACEHOLDER_MARKER = "change-me"
# Bounds of the reservation and payment delays: from a minute to a day.
MIN_DELAY_SECONDS: Final = 60
MAX_DELAY_SECONDS: Final = 86_400
# Words of letters, digits, "_" and "-" joined by ":", as in "livecart" or "livecart:staging".
REDIS_KEY_PREFIX_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]+(:[A-Za-z0-9_-]+)*")
MAX_REDIS_KEY_PREFIX_LENGTH: Final = 64
# A Host header the API answers: a DNS name, possibly under "*." for its
# subdomains, an IPv4 address or a bracketed IPv6 one; lower case, as browsers send it.
ALLOWED_HOST_PATTERN: Final = re.compile(
    r"(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*|\[[0-9a-f:.]+\]"
)
# Upper bound of every rate limit: beyond it a limit no longer limits anything.
MAX_RATE_LIMIT: Final = 1_000_000
RATE_LIMITS: Final = (
    "rate_limit_login_per_minute",
    "rate_limit_login_failures",
    "rate_limit_seller_per_minute",
    "rate_limit_buyer_lookup_per_minute",
    "rate_limit_buyer_lookup_not_found_per_minute",
    "buyer_lookup_not_found_alert_per_minute",
    "rate_limit_buyer_token_per_minute",
    "rate_limit_storefront_per_minute",
    "rate_limit_comment_webhook_per_minute",
)
# The bounds of the comment queue's backpressure settings.
MAX_QUEUE_DEPTH: Final = 1_000_000
# The envelope of a comment of 1000 four-byte characters with ASCII ids of
# full length takes 4706 bytes: a smaller bound would refuse comments the
# webhook format allows.
MIN_QUEUE_ENVELOPE_BYTES: Final = 5_120
MAX_QUEUE_ENVELOPE_BYTES: Final = 65_536
MIN_REDIS_MAXMEMORY_BYTES: Final = 1024 * 1024
# What one queued envelope may cost Redis, as a multiple of its JSON. The
# stream's listpack adds about 50 bytes per entry, which MEMORY USAGE reports;
# jemalloc then rounds each allocation above 4 KiB up to a size class at most
# 25 % above it, which MEMORY USAGE does not show. Measured on redis
# 8.10.2 (jemalloc 5.3.0), 4000 envelopes of 5120 bytes took 6162 bytes of
# used_memory each, 5171 by MEMORY USAGE.
QUEUE_ENTRY_MEMORY_FACTOR: Final = Fraction(5, 4)
# The share of Redis's maxmemory a full queue may take: the rest holds the
# session streams, the rate limit and budget counters, the idempotency keys
# and the history not trimmed yet. Under noeviction, a full Redis refuses
# writes, logins included.
QUEUE_MEMORY_SHARE: Final = Fraction(1, 2)
# One HMAC key per platform's comment webhook (livecart_core.signing).
COMMENT_WEBHOOK_SECRETS: Final = {
    platform: f"comment_webhook_secret_{platform.value}" for platform in Platform
}
# The bounds of the worker's settings. A message deadline shorter than a few
# seconds would cut units that wait on a lock (lock_timeout is 2 s); a longer
# one than five minutes would hold a comment, and its buyer, for as long.
MAX_WORKER_CONCURRENCY: Final = 64
MAX_WORKER_BATCH: Final = 256
MIN_MESSAGE_PROCESS_TIMEOUT_S: Final = 5
MAX_MESSAGE_PROCESS_TIMEOUT_S: Final = 300
# PostgreSQL's 100 connections, less the 3 it keeps for superusers.
MAX_DB_CONNECTIONS: Final = 97


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


def require_currency(value: str) -> str:
    """An ISO 4217 code, such as EUR: the one currency of a deployment."""
    if not re.fullmatch("[A-Z]{3}", value):
        raise ValueError("must be a three-letter upper-case ISO 4217 code, such as EUR")
    return value


def require_scheme(url: SecretStr, *schemes: str) -> SecretStr:
    """Check the URL scheme without ever echoing the URL, which holds a password."""
    scheme, separator, _ = url.get_secret_value().partition("://")
    if not separator or scheme not in schemes:
        raise ValueError(f"must be a URL with the {' or '.join(schemes)} scheme")
    return url


class BaseAppSettings(BaseSettings):
    """Settings common to every process, and the startup validators they share."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # .env also holds variables meant for other processes (tests, compose).
        extra="ignore",
        frozen=True,
        # A validation error must never print raw input values: they include secrets.
        hide_input_in_errors=True,
    )

    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: LogLevel = LogLevel.INFO

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @field_validator("app_env", mode="before")
    @classmethod
    def _lowercase_app_env(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _reject_placeholder_secrets_in_production(self) -> Self:
        """Refuse to start in production while any secret is still a change-me placeholder.

        Every ``SecretStr`` field is checked, so a secret added later is covered
        without touching this validator. Only variable names are reported.
        """
        if not self.is_production:
            return self
        offenders = sorted(
            name.upper()
            for name in type(self).model_fields
            if isinstance(value := getattr(self, name), SecretStr)
            and PLACEHOLDER_MARKER in value.get_secret_value()
        )
        if offenders:
            raise ValueError(
                f"{PLACEHOLDER_MARKER} placeholders are not allowed when APP_ENV=production; "
                f"replace: {', '.join(offenders)}"
            )
        return self


class Settings(BaseAppSettings):
    """Runtime settings of the API and the worker.

    ``DATABASE_URL`` connects as ``livecart_app``, the least-privileged role
    (SELECT, INSERT and UPDATE only).
    """

    database_url: SecretStr
    redis_url: SecretStr
    # The first segment of every Redis key the processes write ({p} in the plan),
    # so that two deployments, or two test runs, can share one Redis.
    redis_key_prefix: str = "livecart"
    # Signs the sellers' access tokens (HS256).
    jwt_secret: SecretStr
    # Derives the buyers' order access tokens (livecart_core.order_tokens).
    order_token_key: SecretStr
    # Touched by the worker heartbeat, read by `livecart-healthcheck heartbeat`.
    worker_heartbeat_file: Path = DEFAULT_HEARTBEAT_FILE
    # Business bound on the quantity of one purchase intent. The domain never
    # reads settings: the use cases hand it to the parser as an argument.
    max_quantity_per_comment: int = 10
    # How long an unpaid reservation holds the stock before the sweep releases it.
    reservation_ttl_seconds: int = 900
    # The one currency of the deployment (ISO 4217), frozen on every order and
    # payment; multi-currency is out of scope.
    default_currency: str = "EUR"
    # Rate limits of the API (livecart_api.rate_limits): the plan's defaults, to
    # recalibrate once measured. Fixed windows of a minute unless named otherwise.
    rate_limit_login_per_minute: int = 10  # per client address
    # Failed logins per (account, client address), per window below: low enough
    # to stop guessing, per address so that nobody else can lock a seller out.
    rate_limit_login_failures: int = 5
    rate_limit_login_failure_window_seconds: int = 900
    rate_limit_seller_per_minute: int = 300  # per seller, on every seller route
    rate_limit_buyer_lookup_per_minute: int = 30  # per client address
    rate_limit_buyer_lookup_not_found_per_minute: int = 20  # per client address
    # Not-found lookups per minute from all addresses together above which a
    # warning is logged: a guess of references spread over many addresses.
    buyer_lookup_not_found_alert_per_minute: int = 100
    rate_limit_buyer_token_per_minute: int = 60  # per order access token
    # Per client address, where the plan names no limit: generous, since every
    # viewer opens the storefront first and a live audience may share one
    # carrier's address (CGNAT), yet a cap on its three queries a request.
    rate_limit_storefront_per_minute: int = 120
    # Per platform, counted once the signature holds, so that only the
    # platform's own calls fill its counter: set above the rate of the planned
    # load test, it stops a runaway sender, while backpressure (503) protects
    # the queue.
    rate_limit_comment_webhook_per_minute: int = 6000
    # The Host headers the API answers (TrustedHostMiddleware), comma-separated
    # in the environment: another Host is refused with 400 before any route
    # runs, so no URL is ever built from a forged one. 127.0.0.1 serves the
    # container healthcheck, api the other containers.
    allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ("localhost", "127.0.0.1", "api")
    # Strict-Transport-Security on every response, once the API is only reached
    # over HTTPS: browsers then refuse plain HTTP to it for a year.
    hsts_enabled: bool = False
    # The platforms whose comment webhook the API serves, comma-separated in the
    # environment. Only the simulator has a real adapter: the others are
    # documented stubs, which stop the API at startup when listed
    # (livecart_core.infrastructure.platforms).
    enabled_platforms: Annotated[tuple[Platform, ...], NoDecode] = (Platform.SIMULATOR,)
    # The HMAC key of each platform's comment webhook, 32 bytes at least. Optional
    # here, since the worker never checks a webhook: the API refuses to start
    # without the key of each platform it serves.
    comment_webhook_secret_simulator: SecretStr | None = None
    comment_webhook_secret_facebook: SecretStr | None = None
    comment_webhook_secret_instagram: SecretStr | None = None
    comment_webhook_secret_tiktok: SecretStr | None = None
    # Backpressure of the comment webhook: once the queue's backlog (comments
    # not acknowledged yet, delivered or not) reaches this depth, the webhook
    # answers 503 until the workers catch up.
    queue_max_depth: int = 20_000
    # The largest envelope, as JSON, the webhook queues; a comment whose
    # envelope is larger is refused (422). With the depth, it bounds the
    # memory the queue may take.
    queue_max_envelope_bytes: int = 5_120
    # Redis's maxmemory (256 MiB in compose, which passes the same value to
    # both): startup checks that a full queue fits in half of it.
    redis_maxmemory_bytes: int = 256 * 1024 * 1024
    # The worker's comment handlers that run at once, each on a pooled
    # connection of its own (Semaphore(WORKER_CONCURRENCY)).
    worker_concurrency: int = 16
    # The deliveries a worker holds at most, read and not finished yet: the
    # COUNT of its XREADGROUP.
    worker_batch: int = 32
    # The deadline of one delivery, from when its handler starts, and the
    # length of the comment's lease, which therefore outlives the handler.
    message_process_timeout_s: int = 30
    # The connections the worker's pool may open at most: startup refuses a
    # WORKER_CONCURRENCY whose pool (livecart_core.infrastructure.db.engine)
    # would open more.
    worker_db_max_connections: int = 22

    @property
    def reservation_ttl(self) -> timedelta:
        return timedelta(seconds=self.reservation_ttl_seconds)

    @property
    def message_process_timeout(self) -> timedelta:
        return timedelta(seconds=self.message_process_timeout_s)

    @property
    def worker_pool(self) -> PoolSize:
        """The worker's pool: a connection per comment handler and per database loop."""
        return worker_pool(self.worker_concurrency)

    def comment_webhook_secret(self, platform: Platform) -> SecretStr | None:
        """``COMMENT_WEBHOOK_SECRET_<PLATFORM>``, or ``None`` when it is not set."""
        secret: SecretStr | None = getattr(self, COMMENT_WEBHOOK_SECRETS[platform])
        return secret

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        return require_scheme(value, "postgresql+asyncpg")

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, value: SecretStr) -> SecretStr:
        return require_scheme(value, "redis", "rediss")

    @field_validator("redis_key_prefix")
    @classmethod
    def _check_redis_key_prefix(cls, value: str) -> str:
        if len(value) > MAX_REDIS_KEY_PREFIX_LENGTH or not REDIS_KEY_PREFIX_PATTERN.fullmatch(
            value
        ):
            raise ValueError(
                f"must be words of letters, digits, '_' and '-' joined by ':', "
                f"{MAX_REDIS_KEY_PREFIX_LENGTH} characters at most"
            )
        return value

    @field_validator("jwt_secret", "order_token_key", *COMMENT_WEBHOOK_SECRETS.values())
    @classmethod
    def _check_key_length(cls, value: SecretStr | None) -> SecretStr | None:
        """An HMAC key shorter than its SHA-256 output weakens it; the length alone is reported."""
        if value is not None and len(value.get_secret_value().encode()) < MIN_KEY_BYTES:
            raise ValueError(f"must be at least {MIN_KEY_BYTES} bytes long")
        return value

    @property
    def queue_memory_bytes(self) -> int:
        """What a queue of ``queue_max_depth`` envelopes of the largest size may cost Redis."""
        cost = self.queue_max_depth * self.queue_max_envelope_bytes * QUEUE_ENTRY_MEMORY_FACTOR
        return int(cost)

    @field_validator("queue_max_depth")
    @classmethod
    def _check_queue_max_depth(cls, value: int) -> int:
        if not 1 <= value <= MAX_QUEUE_DEPTH:
            raise ValueError(f"must be from 1 to {MAX_QUEUE_DEPTH} comments")
        return value

    @field_validator("queue_max_envelope_bytes")
    @classmethod
    def _check_queue_max_envelope_bytes(cls, value: int) -> int:
        if not MIN_QUEUE_ENVELOPE_BYTES <= value <= MAX_QUEUE_ENVELOPE_BYTES:
            raise ValueError(
                f"must be from {MIN_QUEUE_ENVELOPE_BYTES} to {MAX_QUEUE_ENVELOPE_BYTES} bytes"
            )
        return value

    @field_validator("redis_maxmemory_bytes")
    @classmethod
    def _check_redis_maxmemory_bytes(cls, value: int) -> int:
        if value < MIN_REDIS_MAXMEMORY_BYTES:
            raise ValueError(f"must be at least {MIN_REDIS_MAXMEMORY_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def _a_full_queue_fits_in_half_of_redis(self) -> Self:
        """QUEUE_MAX_DEPTH x QUEUE_MAX_ENVELOPE_BYTES x 5/4 within half of maxmemory.

        Backpressure stops the webhook at the depth, so this is the most the
        queue can take; above half, a spike could fill Redis, and noeviction
        would then refuse every write, the logins' counters included.
        """
        budget = int(self.redis_maxmemory_bytes * QUEUE_MEMORY_SHARE)
        if self.queue_memory_bytes > budget:
            raise ValueError(
                "QUEUE_MAX_DEPTH x QUEUE_MAX_ENVELOPE_BYTES x 1.25 (the allocator's rounding) "
                f"is {self.queue_memory_bytes} bytes: it must stay within half of "
                f"REDIS_MAXMEMORY_BYTES, {budget} bytes"
            )
        return self

    @field_validator("worker_concurrency")
    @classmethod
    def _check_worker_concurrency(cls, value: int) -> int:
        if not 1 <= value <= MAX_WORKER_CONCURRENCY:
            raise ValueError(f"must be from 1 to {MAX_WORKER_CONCURRENCY} handlers")
        return value

    @field_validator("worker_batch")
    @classmethod
    def _check_worker_batch(cls, value: int) -> int:
        if not 1 <= value <= MAX_WORKER_BATCH:
            raise ValueError(f"must be from 1 to {MAX_WORKER_BATCH} deliveries")
        return value

    @field_validator("message_process_timeout_s")
    @classmethod
    def _check_message_process_timeout_s(cls, value: int) -> int:
        if not MIN_MESSAGE_PROCESS_TIMEOUT_S <= value <= MAX_MESSAGE_PROCESS_TIMEOUT_S:
            raise ValueError(
                f"must be from {MIN_MESSAGE_PROCESS_TIMEOUT_S} to "
                f"{MAX_MESSAGE_PROCESS_TIMEOUT_S} seconds"
            )
        return value

    @field_validator("worker_db_max_connections")
    @classmethod
    def _check_worker_db_max_connections(cls, value: int) -> int:
        if not 1 <= value <= MAX_DB_CONNECTIONS:
            raise ValueError(f"must be from 1 to {MAX_DB_CONNECTIONS} connections")
        return value

    @model_validator(mode="after")
    def _the_worker_pool_fits_its_cap(self) -> Self:
        """WORKER_CONCURRENCY + 4 loops + 2 on overflow within WORKER_DB_MAX_CONNECTIONS.

        The cap is the worker's share of PostgreSQL's connections: a
        concurrency raised alone would let three workers and the API open
        more than the server accepts, and the last to connect would fail.
        """
        needed = self.worker_pool.max_connections
        if needed > self.worker_db_max_connections:
            raise ValueError(
                f"WORKER_CONCURRENCY={self.worker_concurrency} needs a pool of {needed} "
                f"connections (a handler each, {WORKER_DB_LOOPS} loops, "
                f"{WORKER_POOL_OVERFLOW} on overflow), above "
                f"WORKER_DB_MAX_CONNECTIONS={self.worker_db_max_connections}"
            )
        return self

    @model_validator(mode="after")
    def _keys_are_distinct(self) -> Self:
        """One key per purpose: a leaked JWT secret must not also give every order's token."""
        if self.jwt_secret.get_secret_value() == self.order_token_key.get_secret_value():
            raise ValueError("ORDER_TOKEN_KEY must differ from JWT_SECRET")
        return self

    @field_validator("max_quantity_per_comment")
    @classmethod
    def _check_max_quantity_per_comment(cls, value: int) -> int:
        """Keep the business bound inside the safety bound of the database CHECK.

        A larger value would let the parser accept a quantity that the
        ``purchase_intents`` insert then rejects, long after the comment was
        acknowledged.
        """
        if not 1 <= value <= QUANTITY_CEILING:
            raise ValueError(
                f"must be from 1 to {QUANTITY_CEILING}, the bound of the "
                "purchase_intents quantity CHECK"
            )
        return value

    @field_validator("reservation_ttl_seconds")
    @classmethod
    def _check_reservation_ttl_seconds(cls, value: int) -> int:
        """Long enough to pay from a phone, short enough to free the stock during the live."""
        if not MIN_DELAY_SECONDS <= value <= MAX_DELAY_SECONDS:
            raise ValueError(f"must be from {MIN_DELAY_SECONDS} to {MAX_DELAY_SECONDS} seconds")
        return value

    @field_validator("default_currency")
    @classmethod
    def _check_default_currency(cls, value: str) -> str:
        return require_currency(value)

    @field_validator(*RATE_LIMITS)
    @classmethod
    def _check_rate_limit(cls, value: int) -> int:
        if not 1 <= value <= MAX_RATE_LIMIT:
            raise ValueError(f"must be from 1 to {MAX_RATE_LIMIT} requests per window")
        return value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_allowed_hosts(cls, value: object) -> object:
        """``localhost, 127.0.0.1,api`` from the environment: hosts between commas."""
        if isinstance(value, str):
            return tuple(host.strip().lower() for host in value.split(",") if host.strip())
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _check_allowed_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("must name at least one host")
        if "*" in value:
            raise ValueError("must name the hosts: * would answer any Host header")
        for host in value:
            if not ALLOWED_HOST_PATTERN.fullmatch(host):
                raise ValueError(
                    f"{host!r} is no host: a lower-case DNS name, possibly under *., "
                    "or an IP address"
                )
        return value

    @field_validator("enabled_platforms", mode="before")
    @classmethod
    def _split_enabled_platforms(cls, value: object) -> object:
        """``simulator, facebook`` from the environment: platforms between commas, each once."""
        if isinstance(value, str):
            names = (name.strip().lower() for name in value.split(","))
            return tuple(dict.fromkeys(name for name in names if name))
        return value

    @field_validator("enabled_platforms")
    @classmethod
    def _check_enabled_platforms(cls, value: tuple[Platform, ...]) -> tuple[Platform, ...]:
        if not value:
            raise ValueError("must name at least one platform, such as simulator")
        return value

    @field_validator("rate_limit_login_failure_window_seconds")
    @classmethod
    def _check_login_failure_window(cls, value: int) -> int:
        if not MIN_DELAY_SECONDS <= value <= MAX_DELAY_SECONDS:
            raise ValueError(f"must be from {MIN_DELAY_SECONDS} to {MAX_DELAY_SECONDS} seconds")
        return value


class MigrationSettings(BaseAppSettings):
    """Settings of ``livecart-migrate``, the only process that alters the schema.

    ``MIGRATOR_URL`` connects as ``livecart_migrator``, owner of the schema. The
    API and worker containers never receive it.
    """

    migrator_url: SecretStr

    @field_validator("migrator_url")
    @classmethod
    def _check_migrator_url(cls, value: SecretStr) -> SecretStr:
        return require_scheme(value, "postgresql+asyncpg")


class SeedSettings(BaseAppSettings):
    """Settings of ``livecart-seed``, which loads the demo data as ``livecart_migrator``.

    The migrator, because ``--reset-loadtest`` deletes rows, which
    ``livecart_app`` may not. ``SEED_DEMO_PASSWORD`` is the password of the
    demo sellers; like every secret, its change-me placeholder is refused in
    production.
    """

    migrator_url: SecretStr
    seed_demo_password: SecretStr
    # Prices the demo products, as DEFAULT_CURRENCY does the sellers' own.
    default_currency: str = "EUR"

    @field_validator("migrator_url")
    @classmethod
    def _check_migrator_url(cls, value: SecretStr) -> SecretStr:
        return require_scheme(value, "postgresql+asyncpg")

    @field_validator("default_currency")
    @classmethod
    def _check_default_currency(cls, value: str) -> str:
        return require_currency(value)
