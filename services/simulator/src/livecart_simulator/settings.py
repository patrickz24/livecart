"""The simulator's settings: environment variables first, then ``.env``, then the defaults.

The command line overrides each of them (``livecart-simulate --help``). From
the host, the defaults reach the API published on 127.0.0.1:8000 and read the
secret of ``.env``; the compose service sets ``SIMULATOR_API_URL`` to the API
container instead.
"""

from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import NoDecode

from livecart_core.settings import (
    MAX_REDIS_KEY_PREFIX_LENGTH,
    REDIS_KEY_PREFIX_PATTERN,
    BaseAppSettings,
    require_scheme,
)
from livecart_core.signing import MIN_SECRET_BYTES
from livecart_simulator.comments import DEFAULT_BUYERS, MAX_BUYERS
from livecart_simulator.live import check_rate


class SimulatorSettings(BaseAppSettings):
    """``SIMULATOR_*``, the webhook secret it signs with, and the Redis of the text mode."""

    # The API's base URL, where the webhook is.
    simulator_api_url: str = "http://127.0.0.1:8000"
    # Comments per second, on average.
    simulator_rate: float = 0.2
    # The seeded session commented on (its external live id).
    simulator_live_id: str = "friday-drop"
    # Codes never named, between commas, such as B07,E15.
    simulator_exclude_codes: Annotated[tuple[str, ...], NoDecode] = ()
    simulator_buyers: int = DEFAULT_BUYERS
    # Signs the webhook calls; the API checks them with the same key.
    comment_webhook_secret_simulator: SecretStr | None = None
    # The text mode only: where the worker writes the buyers' messages.
    redis_url: SecretStr | None = None
    redis_key_prefix: str = "livecart"

    @field_validator("simulator_api_url")
    @classmethod
    def _check_api_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must be an http:// or https:// URL")
        return value.rstrip("/")

    @field_validator("simulator_rate")
    @classmethod
    def _check_rate(cls, value: float) -> float:
        return check_rate(value)

    @field_validator("simulator_exclude_codes", mode="before")
    @classmethod
    def _split_codes(cls, value: object) -> object:
        """``B07, e15`` from the environment: codes between commas, in upper case."""
        if isinstance(value, str):
            return tuple(code.strip().upper() for code in value.split(",") if code.strip())
        return value

    @field_validator("simulator_buyers")
    @classmethod
    def _check_buyers(cls, value: int) -> int:
        if not 1 <= value <= MAX_BUYERS:
            raise ValueError(f"must be from 1 to {MAX_BUYERS} buyers")
        return value

    @field_validator("comment_webhook_secret_simulator")
    @classmethod
    def _check_secret(cls, value: SecretStr | None) -> SecretStr | None:
        # The API's own rule for this key; the length alone is reported.
        if value is not None and len(value.get_secret_value().encode()) < MIN_SECRET_BYTES:
            raise ValueError(f"must be at least {MIN_SECRET_BYTES} bytes long")
        return value

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, value: SecretStr | None) -> SecretStr | None:
        return None if value is None else require_scheme(value, "redis", "rediss")

    @field_validator("redis_key_prefix")
    @classmethod
    def _check_prefix(cls, value: str) -> str:
        if len(value) > MAX_REDIS_KEY_PREFIX_LENGTH or not REDIS_KEY_PREFIX_PATTERN.fullmatch(
            value
        ):
            raise ValueError("must be words of letters, digits, '_' and '-' joined by ':'")
        return value
