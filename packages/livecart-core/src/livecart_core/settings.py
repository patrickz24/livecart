"""Typed configuration, validated once when a process starts.

Values come from environment variables first, then from a ``.env`` file in the
working directory: host-run commands start from the repository root, and
containers receive the same variables from compose. Every secret, including
connection URLs that embed a password, is a ``SecretStr``: it never shows up in a
repr, a log line or a validation error.
"""

from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from livecart_core.domain.quantity import QUANTITY_CEILING
from livecart_core.healthcheck import DEFAULT_HEARTBEAT_FILE

# Marker of the development placeholders shipped in .env.example.
PLACEHOLDER_MARKER = "change-me"


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


def _require_scheme(url: SecretStr, *schemes: str) -> SecretStr:
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
    # Touched by the worker heartbeat, read by `livecart-healthcheck heartbeat`.
    worker_heartbeat_file: Path = DEFAULT_HEARTBEAT_FILE
    # Business bound on the quantity of one purchase intent. The domain never
    # reads settings: the use cases hand it to the parser as an argument.
    max_quantity_per_comment: int = 10

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        return _require_scheme(value, "postgresql+asyncpg")

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, value: SecretStr) -> SecretStr:
        return _require_scheme(value, "redis", "rediss")

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


class MigrationSettings(BaseAppSettings):
    """Settings of ``livecart-migrate``, the only process that alters the schema.

    ``MIGRATOR_URL`` connects as ``livecart_migrator``, owner of the schema. The
    API and worker containers never receive it.
    """

    migrator_url: SecretStr

    @field_validator("migrator_url")
    @classmethod
    def _check_migrator_url(cls, value: SecretStr) -> SecretStr:
        return _require_scheme(value, "postgresql+asyncpg")
