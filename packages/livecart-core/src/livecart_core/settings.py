"""Typed configuration, validated once when a process starts.

Values come from environment variables first, then from a ``.env`` file in the
working directory: host-run commands start from the repository root, and
containers receive the same variables from compose. Every secret, including
connection URLs that embed a password, is a ``SecretStr``: it never shows up in a
repr, a log line or a validation error.
"""

import json
import re
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Final, Self
from urllib.parse import SplitResult, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from livecart_core.application.dto import CarrierName, LlmPrice, PaymentProviderName, Platform
from livecart_core.domain.quantity import QUANTITY_CEILING
from livecart_core.healthcheck import DEFAULT_HEARTBEAT_FILE
from livecart_core.infrastructure.db.engine import (
    WORKER_DB_LOOPS,
    WORKER_POOL_OVERFLOW,
    PoolSize,
    worker_pool,
)
from livecart_core.infrastructure.notifier import LINK_SCHEME_PATTERN
from livecart_core.metrics import DEFAULT_METRICS_PORT
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
    "rate_limit_checkout_per_minute",
    "rate_limit_storefront_per_minute",
    "rate_limit_comment_webhook_per_minute",
    "rate_limit_comment_webhook_signature_failures_per_minute",
    "rate_limit_payment_webhook_signature_failures_per_minute",
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
# The bounds of PAYMENT_MAX_ATTEMPTS: one attempt at least, five at most.
MAX_PAYMENT_ATTEMPTS: Final = 5
# The bounds of MOCK_CARRIER_STEP_SECONDS: from a second, to watch a parcel
# delivered within a minute, to a day.
MIN_MOCK_CARRIER_STEP_SECONDS: Final = 1
MAX_MOCK_CARRIER_STEP_SECONDS: Final = 86_400
# The bounds of SHIPMENT_SYNC_INTERVAL_S: past the 5 s a claim of the tracking
# keeps of its lease, which lasts an interval, and an hour at most.
MIN_SHIPMENT_SYNC_INTERVAL_S: Final = 10
MAX_SHIPMENT_SYNC_INTERVAL_S: Final = 3_600
# Stripe's test mode keys: a secret key or a restricted one. A live key
# (sk_live_, rk_live_) is refused: LiveCart takes no real payment.
STRIPE_TEST_KEY_PREFIXES: Final = ("sk_test_", "rk_test_")
STRIPE_SIGNING_KEY_PREFIX: Final = "whsec_"
# Schemes a return URL never takes, whatever ALLOWED_APP_SCHEMES says: the
# web's, which would make the bridge page an open redirect, those a browser
# runs or reads (javascript, data, file...), and Expo Go's, which only
# development takes, on a private address (livecart_core.application.checkout).
FORBIDDEN_APP_SCHEMES: Final = frozenset(
    {"http", "https", "ws", "wss", "ftp", "javascript", "vbscript", "data", "file", "blob"}
    | {"about", "exp"}
)
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
# The most a delivery may keep for its units after the LLM fallback: each
# waits 2 s for a lock and runs 5 s at most (lock_timeout, statement_timeout),
# so a minute is beyond anything they need.
MAX_DB_BUDGET_S: Final = 60
# PostgreSQL's 100 connections, less the 3 it keeps for superusers.
MAX_DB_CONNECTIONS: Final = 97
# The deliveries of one comment before it goes to the dead-letter queue.
MAX_QUEUE_DELIVERIES: Final = 10
# The longest idle time before a reclaim: past an hour, a crashed worker's
# comments would wait longer than any live stream lasts.
MAX_QUEUE_RECLAIM_MIN_IDLE_MS: Final = 3_600_000
# How many times the longest a live worker may hold a delivery the idle time
# before a reclaim is: the margin covers a loop late to go round, a slow read
# and the clocks of Redis and of the worker.
RECLAIM_IDLE_MARGIN: Final = 2
MAX_PORT: Final = 65_535
# Each stream of an instance holds a connection of a Redis pool the streams
# alone use, sized to SSE_MAX_STREAMS, beside the 100 of the process's shared
# pool: 5000 of them stay far below Redis's default maxclients of 10000.
MAX_SSE_STREAMS: Final = 5_000
# The LLM fallback (livecart_worker.llm). A model id as the API names it, such
# as claude-opus-5, which comments.llm_model holds in 128 characters.
LLM_MODEL_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}")
# One more attempt after a 429, a 5xx or a lost connection (the SDK's max_retries).
LLM_MAX_RETRIES: Final = 1
# The time kept for the SDK's backoff before its retry: 0.5 s at most, unless
# the server asks for longer (retry-after), which the cut of the whole call at
# the request's timeout then bounds.
LLM_BACKOFF_MARGIN_S: Final = 2.0
# The longest an attempt may take: past it, a live buyer waits for nothing.
MAX_LLM_CALL_TIMEOUT_S: Final = 8
# How many ambiguous comments one worker asks the LLM about at once
# (asyncio.Semaphore): the rest wait for a slot, LLM_SLOT_WAIT_S at most.
LLM_SLOTS: Final = 4
MAX_LLM_SLOT_WAIT_S: Final = 60
MAX_LLM_CALLS_PER_SESSION: Final = 100_000
MAX_LLM_DAILY_BUDGET_USD: Final = Decimal(1000)
# List prices in US dollars per million input and output tokens, by model id,
# as Anthropic's pricing reference gave them on 2026-09-26: what the cost guard
# counts each call at, an approximation of the bill (no caching, no batch, no
# taxes). claude-opus-4-8 answers the requests claude-opus-5 declines when the
# refusal fallback is on. LLM_PRICES, as JSON, replaces the whole table.
DEFAULT_LLM_PRICES: Final = {
    "claude-opus-5": LlmPrice(input_per_mtok=Decimal(5), output_per_mtok=Decimal(25)),
    "claude-opus-4-8": LlmPrice(input_per_mtok=Decimal(5), output_per_mtok=Decimal(25)),
    "claude-sonnet-5": LlmPrice(input_per_mtok=Decimal(2), output_per_mtok=Decimal(10)),
    "claude-haiku-4-5": LlmPrice(input_per_mtok=Decimal(1), output_per_mtok=Decimal(5)),
}


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class LlmEffort(StrEnum):
    """``output_config.effort`` of the LLM's requests: how much it may think before it answers."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


def require_currency(value: str) -> str:
    """An ISO 4217 code, such as EUR: the one currency of a deployment."""
    if not re.fullmatch("[A-Z]{3}", value):
        raise ValueError("must be a three-letter upper-case ISO 4217 code, such as EUR")
    return value


def _is_http_url(parts: SplitResult) -> bool:
    """An http(s) URL with a host and a valid port, no user and no fragment."""
    try:
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        port_ok = False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and port_ok
        and parts.username is None
        and not parts.fragment
    )


def require_origin(value: str) -> str:
    """An http(s) origin, such as ``http://localhost:8080``: no path, query, fragment or user.

    Returned without a trailing slash, so that a path can follow it as is.
    """
    parts = urlsplit(value.strip())
    if not _is_http_url(parts) or parts.path not in ("", "/") or parts.query:
        raise ValueError(
            "must be an http or https origin such as http://localhost:8080, without a path"
        )
    return f"{parts.scheme}://{parts.netloc}"


def require_http_url(value: str) -> str:
    """An http(s) URL with a host, such as ``http://127.0.0.1:8000/api/v1/...``."""
    value = value.strip()
    if not _is_http_url(urlsplit(value)):
        raise ValueError("must be an http or https URL with a host, without a user or fragment")
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


class LlmSettings(BaseAppSettings):
    """The settings of the optional LLM fallback, which the worker and ``scripts/llm_eval.py`` read.

    ``Settings`` adds the rest of the worker's, and the rule that ties the
    fallback's times to the message deadline.
    """

    # The optional LLM fallback of the worker, for the comments the parser
    # leaves ambiguous (livecart_worker.llm). Without a key, or with
    # LLM_ENABLED=false, the worker runs the parser alone: every ambiguity is
    # then rejected as ambiguous_unresolved. Only the worker needs the key.
    anthropic_api_key: SecretStr | None = None
    llm_enabled: bool = True
    # The model, by the API's id without a date suffix; any change of model
    # goes through scripts/llm_eval.py first (ADR 0006).
    llm_model: str = "claude-opus-5"
    # How much the model may think; None, from an empty LLM_EFFORT, sends no
    # effort, for a model that does not take one.
    llm_effort: LlmEffort | None = LlmEffort.LOW
    # The server-side refusal fallback ("fallbacks": "default"): a request the
    # model's safety classifiers decline runs again on the model Anthropic
    # recommends for it, in the same call. Off for a model that does not take it.
    llm_refusal_fallback: bool = True
    # The longest one attempt of a call may take; the SDK makes one more
    # after a transient failure, within what the message deadline has left.
    llm_call_timeout_s: float = 8.0
    # The cost guard, shared by every worker through Redis: the calls of one
    # live session, and the dollars of one UTC day, each checked before a call,
    # so the calls in flight when the day's budget is reached go past it: up to
    # LLM_SLOTS per worker process, about $0.12 per worker with the defaults
    # (ADR 0006).
    llm_max_calls_per_session: int = 200
    llm_daily_budget_usd: Decimal = Decimal("1.00")
    llm_prices: Annotated[dict[str, LlmPrice], NoDecode] = Field(
        default_factory=lambda: dict(DEFAULT_LLM_PRICES)
    )
    # How long an ambiguous comment waits for one of the worker's LLM slots
    # before it is settled as llm_busy, rather than waiting until its deadline.
    llm_slot_wait_s: float = 7.0

    @property
    def llm_active(self) -> bool:
        """Whether the worker asks the LLM: a key is set and LLM_ENABLED is not false."""
        return self.llm_enabled and self.anthropic_api_key is not None

    @property
    def llm_call_budget_s(self) -> float:
        """LLM_CALL_BUDGET_S, the longest one call may take: its attempts, and the backoff.

        18 s by default: 2 attempts (LLM_MAX_RETRIES + 1) of LLM_CALL_TIMEOUT_S
        (8 s), and LLM_BACKOFF_MARGIN_S (2 s) for the SDK's backoff between them.
        """
        return self.llm_call_timeout_s * (LLM_MAX_RETRIES + 1) + LLM_BACKOFF_MARGIN_S

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _an_empty_key_is_no_key(cls, value: object) -> object:
        """``ANTHROPIC_API_KEY=``, as .env.example ships it: the parser alone."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("llm_effort", mode="before")
    @classmethod
    def _read_llm_effort(cls, value: object) -> object:
        """``LLM_EFFORT=Low`` is ``low``; an empty ``LLM_EFFORT=`` sends no effort."""
        if isinstance(value, str):
            return value.strip().lower() or None
        return value

    @field_validator("llm_model")
    @classmethod
    def _check_llm_model(cls, value: str) -> str:
        if not LLM_MODEL_PATTERN.fullmatch(value):
            raise ValueError(
                "must be a model id of lower-case letters, digits, '.' and '-', such as "
                "claude-opus-5, 128 characters at most"
            )
        return value

    @field_validator("llm_call_timeout_s")
    @classmethod
    def _check_llm_call_timeout_s(cls, value: float) -> float:
        if not 1 <= value <= MAX_LLM_CALL_TIMEOUT_S:
            raise ValueError(f"must be from 1 to {MAX_LLM_CALL_TIMEOUT_S} seconds")
        return value

    @field_validator("llm_max_calls_per_session")
    @classmethod
    def _check_llm_max_calls_per_session(cls, value: int) -> int:
        if not 1 <= value <= MAX_LLM_CALLS_PER_SESSION:
            raise ValueError(f"must be from 1 to {MAX_LLM_CALLS_PER_SESSION} calls")
        return value

    @field_validator("llm_daily_budget_usd")
    @classmethod
    def _check_llm_daily_budget_usd(cls, value: Decimal) -> Decimal:
        if not 0 < value <= MAX_LLM_DAILY_BUDGET_USD:
            raise ValueError(f"must be above 0 and at most {MAX_LLM_DAILY_BUDGET_USD} dollars")
        return value

    @field_validator("llm_prices", mode="before")
    @classmethod
    def _read_llm_prices(cls, value: object) -> object:
        """LLM_PRICES as JSON; empty, as compose passes it unset, keeps the default table."""
        if isinstance(value, str):
            return json.loads(value) if value.strip() else dict(DEFAULT_LLM_PRICES)
        return value

    @field_validator("llm_prices")
    @classmethod
    def _check_llm_prices(cls, value: dict[str, LlmPrice]) -> dict[str, LlmPrice]:
        for model in value:
            if not LLM_MODEL_PATTERN.fullmatch(model):
                raise ValueError(f"{model!r} is no model id")
        return value

    @field_validator("llm_slot_wait_s")
    @classmethod
    def _check_llm_slot_wait_s(cls, value: float) -> float:
        if not 1 <= value <= MAX_LLM_SLOT_WAIT_S:
            raise ValueError(f"must be from 1 to {MAX_LLM_SLOT_WAIT_S} seconds")
        return value

    @model_validator(mode="after")
    def _the_model_has_a_price(self) -> Self:
        """With the fallback on, LLM_MODEL in LLM_PRICES: the daily budget counts its calls."""
        if self.llm_active and self.llm_model not in self.llm_prices:
            raise ValueError(
                f"LLM_MODEL={self.llm_model} has no price in LLM_PRICES: the daily budget "
                "could not count its calls"
            )
        return self


class Settings(LlmSettings):
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
    # Checkouts per minute per order access token, within the 60 above: each
    # one may call the payment provider.
    rate_limit_checkout_per_minute: int = 5
    # Per client address, where the plan names no limit: generous, since every
    # viewer opens the storefront first and a live audience may share one
    # carrier's address (CGNAT), yet a cap on its three queries a request.
    rate_limit_storefront_per_minute: int = 120
    # Per platform, counted once the signature holds, so that only the
    # platform's own calls fill its counter: set above the rate of the planned
    # load test, it stops a runaway sender, while backpressure (503) protects
    # the queue.
    rate_limit_comment_webhook_per_minute: int = 6000
    # Per client address: the webhook calls whose signature fails. Past it, the
    # address gets 429 before its body is read or its HMAC computed, so that
    # unsigned calls cannot make the API read and hash 1 MiB bodies at will; a
    # platform whose signature holds never fills it.
    rate_limit_comment_webhook_signature_failures_per_minute: int = 60
    # Per client address: the payment webhook calls whose signature fails, as
    # the comment webhook's. A provider whose signature holds never fills it,
    # and a Redis outage never stops a payment webhook: the rule fails open.
    rate_limit_payment_webhook_signature_failures_per_minute: int = 60
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
    # The payment provider of the hosted checkout (livecart_core.infrastructure
    # .payments): the mock by default, which needs no account and no key;
    # PayPal and Viva are documented stubs that stop the process at startup.
    payment_provider: PaymentProviderName = PaymentProviderName.MOCK
    # The mock's HMAC key, 32 bytes at least: it signs the webhook the mock's
    # hosted page sends when the buyer pays, and names its sessions, in the API
    # and the worker alike. Required while PAYMENT_PROVIDER=mock.
    mock_payment_webhook_secret: SecretStr | None = None
    # Stripe Checkout in test mode, both required while PAYMENT_PROVIDER=stripe:
    # the secret key of a test account (sk_test_..., or a restricted rk_test_...),
    # and the signing secret (whsec_...) of the webhook endpoint that posts to
    # /api/v1/webhooks/payments/stripe, which construct_event checks.
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    # The origin the buyer's browser reaches the API on, through nginx: the
    # provider sends the buyer back to its bridge page there, and the mock's
    # hosted page is served there. In development, the app's EXPO_PUBLIC_API_URL.
    public_api_base_url: str = "http://localhost:8080"
    # Where the API posts the webhook of a mock payment: its own payment
    # webhook, on the loopback of its container (or of the host that runs it).
    mock_payment_webhook_url: str = "http://127.0.0.1:8000/api/v1/webhooks/payments/mock"
    # The time a payment attempt adds to its order's reservation, at least:
    # opening one extends the reservation to now + PAYMENT_GRACE_SECONDS if it
    # would end sooner, so that a buyer who reached the payment page has the
    # time to pay.
    payment_grace_seconds: int = 600
    # The payment attempts of one order: a new one opens once the previous
    # attempt's session expired or its payment failed.
    payment_max_attempts: int = 3
    # The URI schemes of the buyer apps' return links (<scheme>://...), where
    # the bridge page sends the buyer back after the payment: the brands'
    # schemes, comma-separated in the environment. Expo Go's exp:// is taken
    # too in development, on a private address only.
    allowed_app_schemes: Annotated[tuple[str, ...], NoDecode] = ("livecart", "samplelive")
    # The carrier of the paid orders' shipments (livecart_core.infrastructure
    # .carriers): the mock by default, which needs no account and keeps no
    # state; DPD, Chronopost and DHL are documented stubs that stop the worker,
    # the one process that calls the carrier, at startup.
    carrier_provider: CarrierName = CarrierName.MOCK
    # The mock carrier's HMAC key, 32 bytes at least: it derives the carrier
    # ref and the tracking number of each order's shipment. Required by the
    # worker while CARRIER_PROVIDER=mock.
    mock_carrier_secret: SecretStr | None = None
    # How long a parcel of the mock carrier stays at each step of its route
    # (created, in transit, out for delivery, then delivered for good).
    mock_carrier_step_seconds: int = 120
    # How often each worker's shipments loop runs: the fallback that ships the
    # paid orders left without a shipment, then the tracking, which asks the
    # carrier about the shipments in flight, each leased to one worker for
    # this long.
    shipment_sync_interval_s: int = 60
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
    # The part of a delivery's deadline kept for what follows the LLM
    # fallback (its receipt, unit 3, the events, the buyer's notice): the
    # fallback gets what is left of the deadline, less this.
    db_budget_s: int = 5
    # The connections the worker's pool may open at most: startup refuses a
    # WORKER_CONCURRENCY whose pool (livecart_core.infrastructure.db.engine)
    # would open more.
    worker_db_max_connections: int = 22
    # Deliveries of one comment: the next one goes to the dead-letter queue
    # instead, and the comment is settled as dead_lettered.
    queue_max_deliveries: int = 3
    # The URI scheme of the buyer app's links, <scheme>://link/<reference>#t=<token>:
    # the demo brand's (milestone 8 gives each brand its own).
    buyer_link_scheme: str = "livecart"
    # How long a delivery stays pending, unacknowledged, before another
    # consumer reclaims it (XAUTOCLAIM). None derives it from the worker's
    # window (queue_reclaim_min_idle); a value set must not be shorter.
    queue_reclaim_min_idle_ms: int | None = None
    # The internal port of the process's Prometheus endpoint (livecart_core.metrics),
    # never published on the host nor routed by nginx. None, from an empty
    # METRICS_PORT, serves no endpoint: the tests, a second process on one host.
    metrics_port: int | None = DEFAULT_METRICS_PORT
    # The streams of session events one API instance serves at once (SSE,
    # GET /api/v1/live-sessions/{id}/events): each holds a connection blocked
    # in XREAD, from a Redis pool of this many connections that only the streams
    # use, so that they never starve the shared pool's 100 (the webhook, the
    # login, /ready); the next one answers 503 with Retry-After.
    sse_max_streams: int = 200
    # The streams one seller may hold open on an instance at once: a dashboard
    # needs one or two, and a stream whose client vanished keeps its slot until
    # the server notices, when a keep-alive fails to go out. The next one
    # answers 429 with Retry-After, so that one seller never takes every slot
    # of an instance.
    sse_max_streams_per_seller: int = 5

    @property
    def reservation_ttl(self) -> timedelta:
        return timedelta(seconds=self.reservation_ttl_seconds)

    @property
    def payment_grace(self) -> timedelta:
        return timedelta(seconds=self.payment_grace_seconds)

    @property
    def mock_carrier_step(self) -> timedelta:
        return timedelta(seconds=self.mock_carrier_step_seconds)

    @property
    def shipment_sync_interval(self) -> timedelta:
        return timedelta(seconds=self.shipment_sync_interval_s)

    @property
    def message_process_timeout(self) -> timedelta:
        return timedelta(seconds=self.message_process_timeout_s)

    @property
    def db_budget(self) -> timedelta:
        return timedelta(seconds=self.db_budget_s)

    @property
    def worker_pool(self) -> PoolSize:
        """The worker's pool: a connection per comment handler and per database loop."""
        return worker_pool(self.worker_concurrency)

    @property
    def longest_pending(self) -> timedelta:
        """The longest a live worker holds a delivery: ceil(batch / concurrency) deadlines.

        A worker holds at most WORKER_BATCH deliveries and runs
        WORKER_CONCURRENCY of them at once, each within its deadline: the last
        one read waits for the others at most ceil(batch / concurrency) - 1
        deadlines, then runs its own.
        """
        rounds = -(-self.worker_batch // self.worker_concurrency)
        return rounds * self.message_process_timeout

    @property
    def queue_reclaim_min_idle(self) -> timedelta:
        """QUEUE_RECLAIM_MIN_IDLE_MS, or twice the longest a live worker holds a delivery.

        120 s by default: 2 x ceil(32 / 16) x 30 s. A shorter idle time would
        reclaim deliveries a live worker still holds, and count deliveries
        towards the dead-letter queue that never failed.
        """
        if self.queue_reclaim_min_idle_ms is not None:
            return timedelta(milliseconds=self.queue_reclaim_min_idle_ms)
        return RECLAIM_IDLE_MARGIN * self.longest_pending

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

    @field_validator(
        "jwt_secret",
        "order_token_key",
        "mock_payment_webhook_secret",
        "mock_carrier_secret",
        *COMMENT_WEBHOOK_SECRETS.values(),
    )
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

    @model_validator(mode="after")
    def _the_llm_fits_the_deadline(self) -> Self:
        """With the fallback on: MESSAGE_PROCESS_TIMEOUT_S >= LLM_CALL_BUDGET_S + DB_BUDGET_S +
        LLM_SLOT_WAIT_S.

        30 >= 18 + 5 + 7 by default. A comment that waited the longest for a
        slot, then made the longest call, still has the time of its units
        before the consumer cancels it: a deadline cut shorter would cancel
        deliveries whose call was paid for, and redeliver them. The parser
        alone needs no such time, so a worker without a key skips the rule.
        """
        if not self.llm_active:
            return self
        needed = self.llm_call_budget_s + self.db_budget_s + self.llm_slot_wait_s
        if self.message_process_timeout_s < needed:
            raise ValueError(
                f"MESSAGE_PROCESS_TIMEOUT_S={self.message_process_timeout_s} is shorter than "
                f"LLM_CALL_BUDGET_S ({self.llm_call_budget_s:g}: LLM_CALL_TIMEOUT_S x "
                f"{LLM_MAX_RETRIES + 1} + {LLM_BACKOFF_MARGIN_S:g}) + DB_BUDGET_S "
                f"({self.db_budget_s}) + LLM_SLOT_WAIT_S ({self.llm_slot_wait_s:g}) = {needed:g}"
            )
        return self

    @field_validator("db_budget_s")
    @classmethod
    def _check_db_budget_s(cls, value: int) -> int:
        """A second at least: the fallback must never take the time its receipt needs."""
        if not 1 <= value <= MAX_DB_BUDGET_S:
            raise ValueError(f"must be from 1 to {MAX_DB_BUDGET_S} seconds")
        return value

    @field_validator("worker_db_max_connections")
    @classmethod
    def _check_worker_db_max_connections(cls, value: int) -> int:
        if not 1 <= value <= MAX_DB_CONNECTIONS:
            raise ValueError(f"must be from 1 to {MAX_DB_CONNECTIONS} connections")
        return value

    @field_validator("buyer_link_scheme")
    @classmethod
    def _check_buyer_link_scheme(cls, value: str) -> str:
        if not LINK_SCHEME_PATTERN.fullmatch(value):
            raise ValueError("must be a URI scheme: a lower-case letter, then letters, digits, +-.")
        return value

    @field_validator("queue_reclaim_min_idle_ms", mode="before")
    @classmethod
    def _derive_an_empty_reclaim_idle_time(cls, value: object) -> object:
        """``QUEUE_RECLAIM_MIN_IDLE_MS=`` derives it too: compose passes the variable unset so."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("metrics_port", mode="before")
    @classmethod
    def _disable_metrics_on_an_empty_port(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("metrics_port")
    @classmethod
    def _check_metrics_port(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= MAX_PORT:
            raise ValueError(f"must be a TCP port from 1 to {MAX_PORT}, or empty to serve none")
        return value

    @field_validator("sse_max_streams", "sse_max_streams_per_seller")
    @classmethod
    def _check_sse_max_streams(cls, value: int) -> int:
        if not 1 <= value <= MAX_SSE_STREAMS:
            raise ValueError(f"must be from 1 to {MAX_SSE_STREAMS} streams")
        return value

    @field_validator("queue_max_deliveries")
    @classmethod
    def _check_queue_max_deliveries(cls, value: int) -> int:
        if not 1 <= value <= MAX_QUEUE_DELIVERIES:
            raise ValueError(f"must be from 1 to {MAX_QUEUE_DELIVERIES} deliveries")
        return value

    @model_validator(mode="after")
    def _a_reclaim_never_takes_a_live_delivery(self) -> Self:
        """QUEUE_RECLAIM_MIN_IDLE_MS, if set, at least twice the longest a live worker holds one."""
        if self.queue_reclaim_min_idle_ms is None:
            return self
        derived = RECLAIM_IDLE_MARGIN * self.longest_pending
        shortest = int(derived.total_seconds() * 1000)
        if not shortest <= self.queue_reclaim_min_idle_ms <= MAX_QUEUE_RECLAIM_MIN_IDLE_MS:
            raise ValueError(
                f"QUEUE_RECLAIM_MIN_IDLE_MS must be from {shortest} (twice "
                "ceil(WORKER_BATCH / WORKER_CONCURRENCY) x MESSAGE_PROCESS_TIMEOUT_S) to "
                f"{MAX_QUEUE_RECLAIM_MIN_IDLE_MS} ms, not {self.queue_reclaim_min_idle_ms}"
            )
        return self

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

    @field_validator("payment_provider", "carrier_provider", mode="before")
    @classmethod
    def _lowercase_provider(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("stripe_secret_key", "stripe_webhook_secret", mode="before")
    @classmethod
    def _an_empty_stripe_value_is_none(cls, value: object) -> object:
        """``STRIPE_SECRET_KEY=``, as .env.example ships it and compose passes it: no Stripe."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("stripe_secret_key")
    @classmethod
    def _check_stripe_secret_key(cls, value: SecretStr | None) -> SecretStr | None:
        """A test mode key only; the message never repeats the key."""
        if value is not None and not value.get_secret_value().startswith(STRIPE_TEST_KEY_PREFIXES):
            raise ValueError(
                "must be a Stripe test mode key (sk_test_... or rk_test_...): "
                "LiveCart takes no live payment"
            )
        return value

    @field_validator("stripe_webhook_secret")
    @classmethod
    def _check_stripe_webhook_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith(STRIPE_SIGNING_KEY_PREFIX):
            raise ValueError("must be the signing secret of a Stripe webhook endpoint (whsec_...)")
        return value

    @field_validator("public_api_base_url")
    @classmethod
    def _check_public_api_base_url(cls, value: str) -> str:
        return require_origin(value)

    @field_validator("mock_payment_webhook_url")
    @classmethod
    def _check_mock_payment_webhook_url(cls, value: str) -> str:
        return require_http_url(value)

    @field_validator("payment_grace_seconds")
    @classmethod
    def _check_payment_grace_seconds(cls, value: int) -> int:
        if not MIN_DELAY_SECONDS <= value <= MAX_DELAY_SECONDS:
            raise ValueError(f"must be from {MIN_DELAY_SECONDS} to {MAX_DELAY_SECONDS} seconds")
        return value

    @field_validator("payment_max_attempts")
    @classmethod
    def _check_payment_max_attempts(cls, value: int) -> int:
        if not 1 <= value <= MAX_PAYMENT_ATTEMPTS:
            raise ValueError(f"must be from 1 to {MAX_PAYMENT_ATTEMPTS} attempts")
        return value

    @field_validator("shipment_sync_interval_s")
    @classmethod
    def _check_shipment_sync_interval_s(cls, value: int) -> int:
        if not MIN_SHIPMENT_SYNC_INTERVAL_S <= value <= MAX_SHIPMENT_SYNC_INTERVAL_S:
            raise ValueError(
                f"must be from {MIN_SHIPMENT_SYNC_INTERVAL_S} to "
                f"{MAX_SHIPMENT_SYNC_INTERVAL_S} seconds"
            )
        return value

    @field_validator("mock_carrier_step_seconds")
    @classmethod
    def _check_mock_carrier_step_seconds(cls, value: int) -> int:
        if not MIN_MOCK_CARRIER_STEP_SECONDS <= value <= MAX_MOCK_CARRIER_STEP_SECONDS:
            raise ValueError(
                f"must be from {MIN_MOCK_CARRIER_STEP_SECONDS} to "
                f"{MAX_MOCK_CARRIER_STEP_SECONDS} seconds"
            )
        return value

    @field_validator("allowed_app_schemes", mode="before")
    @classmethod
    def _split_allowed_app_schemes(cls, value: object) -> object:
        """``livecart, samplelive`` from the environment: schemes between commas, each once."""
        if isinstance(value, str):
            names = (name.strip().lower() for name in value.split(","))
            return tuple(dict.fromkeys(name for name in names if name))
        return value

    @field_validator("allowed_app_schemes")
    @classmethod
    def _check_allowed_app_schemes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("must name at least one scheme, such as livecart")
        for scheme in value:
            if not LINK_SCHEME_PATTERN.fullmatch(scheme):
                raise ValueError(f"{scheme!r} is not a URI scheme")
            if scheme in FORBIDDEN_APP_SCHEMES:
                raise ValueError(f"{scheme} is no app's scheme: a return URL never takes it")
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
