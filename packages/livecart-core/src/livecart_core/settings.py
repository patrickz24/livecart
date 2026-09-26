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
from pathlib import Path
from typing import Final, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from livecart_core.domain.quantity import QUANTITY_CEILING
from livecart_core.healthcheck import DEFAULT_HEARTBEAT_FILE
from livecart_core.order_tokens import MIN_KEY_BYTES

# Marker of the development placeholders shipped in .env.example.
PLACEHOLDER_MARKER = "change-me"
# Bounds of the reservation and payment delays: from a minute to a day.
MIN_DELAY_SECONDS: Final = 60
MAX_DELAY_SECONDS: Final = 86_400
# Words of letters, digits, "_" and "-" joined by ":", as in "livecart" or "livecart:staging".
REDIS_KEY_PREFIX_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]+(:[A-Za-z0-9_-]+)*")
MAX_REDIS_KEY_PREFIX_LENGTH: Final = 64
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
)


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


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

    @property
    def reservation_ttl(self) -> timedelta:
        return timedelta(seconds=self.reservation_ttl_seconds)

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

    @field_validator("jwt_secret", "order_token_key")
    @classmethod
    def _check_key_length(cls, value: SecretStr) -> SecretStr:
        """An HMAC key shorter than its SHA-256 output weakens it; the length alone is reported."""
        if len(value.get_secret_value().encode()) < MIN_KEY_BYTES:
            raise ValueError(f"must be at least {MIN_KEY_BYTES} bytes long")
        return value

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
        if not re.fullmatch("[A-Z]{3}", value):
            raise ValueError("must be a three-letter upper-case ISO 4217 code, such as EUR")
        return value

    @field_validator(*RATE_LIMITS)
    @classmethod
    def _check_rate_limit(cls, value: int) -> int:
        if not 1 <= value <= MAX_RATE_LIMIT:
            raise ValueError(f"must be from 1 to {MAX_RATE_LIMIT} requests per window")
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
