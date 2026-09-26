from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from livecart_core.application.dto import Platform
from livecart_core.healthcheck import DEFAULT_HEARTBEAT_FILE
from livecart_core.settings import (
    COMMENT_WEBHOOK_SECRETS,
    RATE_LIMITS,
    AppEnv,
    LogLevel,
    MigrationSettings,
    SeedSettings,
    Settings,
)
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY

APP_URL = "postgresql+asyncpg://livecart_app:{password}@localhost:55432/livecart"
REDIS_URL = "redis://:{password}@localhost:56379/0"
WORKER_SETTINGS = (
    "WORKER_CONCURRENCY",
    "WORKER_BATCH",
    "MESSAGE_PROCESS_TIMEOUT_S",
    "QUEUE_MAX_DELIVERIES",
    "QUEUE_RECLAIM_MIN_IDLE_MS",
    "BUYER_LINK_SCHEME",
)
# Signing keys that pass every check, in production too.
KEYS = {
    "JWT_SECRET": TEST_JWT_SECRET.get_secret_value(),
    "ORDER_TOKEN_KEY": TEST_ORDER_TOKEN_KEY.get_secret_value(),
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from an environment without any LiveCart variable."""
    names = (
        "APP_ENV",
        "LOG_LEVEL",
        "DATABASE_URL",
        "REDIS_URL",
        "MIGRATOR_URL",
        "SEED_DEMO_PASSWORD",
    )
    extra = ("WORKER_HEARTBEAT_FILE", "MAX_QUANTITY_PER_COMMENT", "RESERVATION_TTL_SECONDS")
    keys = ("JWT_SECRET", "ORDER_TOKEN_KEY")
    limits = (*(name.upper() for name in RATE_LIMITS), "RATE_LIMIT_LOGIN_FAILURE_WINDOW_SECONDS")
    http = ("ALLOWED_HOSTS", "HSTS_ENABLED")
    webhooks = ("ENABLED_PLATFORMS", *(name.upper() for name in COMMENT_WEBHOOK_SECRETS.values()))
    webhooks += ("QUEUE_MAX_DEPTH", "QUEUE_MAX_ENVELOPE_BYTES", "REDIS_MAXMEMORY_BYTES")
    worker = (*WORKER_SETTINGS, "WORKER_DB_MAX_CONNECTIONS")
    for name in (
        *names,
        *extra,
        *keys,
        *limits,
        *http,
        *webhooks,
        *worker,
        "DEFAULT_CURRENCY",
        "REDIS_KEY_PREFIX",
        "METRICS_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def load(**env: str) -> Settings:
    """Build Settings from explicit values only, never from the repository .env."""
    with pytest.MonkeyPatch.context() as patch:
        for name, value in env.items():
            patch.setenv(name, value)
        return Settings(_env_file=None)


def dev_env(
    password: str = "change-me-app", redis_password: str = "change-me-redis"
) -> dict[str, str]:
    return {
        "DATABASE_URL": APP_URL.format(password=password),
        "REDIS_URL": REDIS_URL.format(password=redis_password),
        **KEYS,
    }


def _key_lines() -> str:
    return "".join(f"{name}={value}\n" for name, value in KEYS.items())


def test_defaults_and_types() -> None:
    settings = load(**dev_env())

    assert settings.app_env is AppEnv.DEVELOPMENT
    assert settings.log_level is LogLevel.INFO
    assert isinstance(settings.database_url, SecretStr)
    assert settings.database_url.get_secret_value() == APP_URL.format(password="change-me-app")
    assert not settings.is_production


def test_secrets_never_appear_in_repr() -> None:
    settings = load(**dev_env(password="s3cr3t-db", redis_password="s3cr3t-redis"))

    assert "s3cr3t" not in repr(settings)
    assert "s3cr3t" not in str(settings.model_dump())


def test_env_values_are_case_insensitive() -> None:
    settings = load(APP_ENV="Production", LOG_LEVEL="debug", **dev_env("real", "real"))

    assert settings.app_env is AppEnv.PRODUCTION
    assert settings.log_level is LogLevel.DEBUG


def test_missing_setting_fails_without_echoing_other_values() -> None:
    with pytest.raises(ValidationError) as excinfo:
        load(DATABASE_URL=APP_URL.format(password="s3cr3t-db"))

    message = str(excinfo.value)
    assert "redis_url" in message
    assert "s3cr3t" not in message


def test_placeholders_are_accepted_in_development() -> None:
    settings = load(APP_ENV="development", **dev_env())

    assert "change-me" in settings.redis_url.get_secret_value()


def test_placeholders_are_rejected_in_production() -> None:
    with pytest.raises(ValidationError) as excinfo:
        load(APP_ENV="production", **dev_env(password="change-me-app", redis_password="real"))

    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "REDIS_URL" not in message
    assert "change-me-app@" not in message


def test_production_starts_with_real_secrets() -> None:
    settings = load(APP_ENV="production", **dev_env(password="p4ss", redis_password="p4ss"))

    assert settings.is_production


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://livecart_app:s3cr3t@localhost/livecart",
        "postgresql+psycopg://livecart_app:s3cr3t@localhost/livecart",
        "livecart_app:s3cr3t@localhost/livecart",
    ],
)
def test_database_url_must_use_asyncpg(database_url: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        load(DATABASE_URL=database_url, REDIS_URL=REDIS_URL.format(password="x"), **KEYS)

    assert "postgresql+asyncpg" in str(excinfo.value)
    assert "s3cr3t" not in str(excinfo.value)


def test_redis_url_must_use_a_redis_scheme() -> None:
    with pytest.raises(ValidationError, match="redis or rediss"):
        load(DATABASE_URL=APP_URL.format(password="x"), REDIS_URL="http://localhost:6379", **KEYS)


def test_tls_redis_is_accepted() -> None:
    settings = load(
        DATABASE_URL=APP_URL.format(password="x"), REDIS_URL="rediss://:x@cache:6380/0", **KEYS
    )

    assert settings.redis_url.get_secret_value().startswith("rediss://")


def test_unknown_values_are_rejected() -> None:
    with pytest.raises(ValidationError, match="app_env"):
        load(APP_ENV="staging", **dev_env())


def test_reads_dotenv_and_ignores_foreign_variables(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        f"DATABASE_URL={APP_URL.format(password='from-file')}\n"
        f"REDIS_URL={REDIS_URL.format(password='from-file')}\n"
        "LOG_LEVEL=warning\n"
        "SOME_OTHER_PROCESS_VARIABLE=ignored\n" + _key_lines(),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.log_level is LogLevel.WARNING
    assert "from-file" in settings.database_url.get_secret_value()


def test_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DATABASE_URL={APP_URL.format(password='x')}\nREDIS_URL=redis://file:6379/0\n"
        + _key_lines(),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDIS_URL", "redis://environment:6379/0")

    settings = Settings(_env_file=env_file)

    assert settings.redis_url.get_secret_value() == "redis://environment:6379/0"


def test_worker_heartbeat_file_defaults_to_the_healthcheck_path(tmp_path: Path) -> None:
    assert load(**dev_env()).worker_heartbeat_file == DEFAULT_HEARTBEAT_FILE

    custom = tmp_path / "beat"
    settings = load(WORKER_HEARTBEAT_FILE=str(custom), **dev_env())

    assert settings.worker_heartbeat_file == custom


def test_settings_are_immutable() -> None:
    settings = load(**dev_env())

    with pytest.raises(ValidationError):
        # mypy already rejects the assignment (frozen model); pydantic must too.
        settings.log_level = LogLevel.DEBUG  # type: ignore[misc]


def test_max_quantity_per_comment_defaults_to_ten() -> None:
    assert load(**dev_env()).max_quantity_per_comment == 10


@pytest.mark.parametrize("value", ["1", "10", "99"])
def test_max_quantity_per_comment_accepts_the_database_check_range(value: str) -> None:
    settings = load(MAX_QUANTITY_PER_COMMENT=value, **dev_env())

    assert settings.max_quantity_per_comment == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "100", "1000"])
def test_max_quantity_per_comment_must_stay_within_the_database_check(value: str) -> None:
    with pytest.raises(ValidationError, match="from 1 to 99") as excinfo:
        load(MAX_QUANTITY_PER_COMMENT=value, **dev_env())

    assert "max_quantity_per_comment" in str(excinfo.value)


def test_max_quantity_per_comment_must_be_an_integer() -> None:
    with pytest.raises(ValidationError, match="max_quantity_per_comment"):
        load(MAX_QUANTITY_PER_COMMENT="ten", **dev_env())


def test_a_reservation_holds_the_stock_for_fifteen_minutes_by_default() -> None:
    settings = load(**dev_env())

    assert settings.reservation_ttl_seconds == 900
    assert settings.reservation_ttl == timedelta(minutes=15)


@pytest.mark.parametrize("value", ["60", "1800", "86400"])
def test_the_reservation_ttl_is_configurable(value: str) -> None:
    assert load(RESERVATION_TTL_SECONDS=value, **dev_env()).reservation_ttl_seconds == int(value)


@pytest.mark.parametrize("value", ["59", "0", "-900", "86401"])
def test_the_reservation_ttl_stays_between_a_minute_and_a_day(value: str) -> None:
    with pytest.raises(ValidationError, match="from 60 to 86400 seconds") as excinfo:
        load(RESERVATION_TTL_SECONDS=value, **dev_env())

    assert "reservation_ttl_seconds" in str(excinfo.value)


def test_the_deployment_currency_is_the_euro_by_default() -> None:
    assert load(**dev_env()).default_currency == "EUR"
    assert load(DEFAULT_CURRENCY="CHF", **dev_env()).default_currency == "CHF"


@pytest.mark.parametrize("value", ["eur", "EURO", "E1R", ""])
def test_the_deployment_currency_is_an_iso_4217_code(value: str) -> None:
    with pytest.raises(ValidationError, match="ISO 4217"):
        load(DEFAULT_CURRENCY=value, **dev_env())


def test_the_rate_limits_default_to_the_plan() -> None:
    settings = load(**dev_env())

    assert settings.rate_limit_login_per_minute == 10
    assert settings.rate_limit_login_failures == 5
    assert settings.rate_limit_login_failure_window_seconds == 900
    assert settings.rate_limit_seller_per_minute == 300
    assert settings.rate_limit_buyer_lookup_per_minute == 30
    assert settings.rate_limit_buyer_lookup_not_found_per_minute == 20
    assert settings.buyer_lookup_not_found_alert_per_minute == 100
    assert settings.rate_limit_buyer_token_per_minute == 60
    assert settings.rate_limit_storefront_per_minute == 120
    assert settings.rate_limit_comment_webhook_per_minute == 6000


@pytest.mark.parametrize("name", RATE_LIMITS)
def test_every_rate_limit_is_configurable_within_its_bounds(name: str) -> None:
    assert getattr(load(**{name.upper(): "1"}, **dev_env()), name) == 1
    assert getattr(load(**{name.upper(): "1000000"}, **dev_env()), name) == 1_000_000
    for refused in ("0", "-5", "1000001"):
        with pytest.raises(ValidationError, match="from 1 to 1000000 requests per window"):
            load(**{name.upper(): refused}, **dev_env())


@pytest.mark.parametrize("value", ["59", "86401"])
def test_the_login_failure_window_stays_between_a_minute_and_a_day(value: str) -> None:
    assert load(RATE_LIMIT_LOGIN_FAILURE_WINDOW_SECONDS="60", **dev_env())
    with pytest.raises(ValidationError, match="from 60 to 86400 seconds"):
        load(RATE_LIMIT_LOGIN_FAILURE_WINDOW_SECONDS=value, **dev_env())


def test_the_api_answers_the_local_hosts_and_sends_no_hsts_by_default() -> None:
    settings = load(**dev_env())

    assert settings.allowed_hosts == ("localhost", "127.0.0.1", "api")
    assert settings.hsts_enabled is False
    assert load(HSTS_ENABLED="true", **dev_env()).hsts_enabled is True


def test_allowed_hosts_are_listed_between_commas() -> None:
    settings = load(
        ALLOWED_HOSTS=" Shop.Example.com, 192.168.1.20 ,*.example.org,[::1],", **dev_env()
    )

    assert settings.allowed_hosts == ("shop.example.com", "192.168.1.20", "*.example.org", "[::1]")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "at least one host"),
        (" , ", "at least one host"),
        ("localhost,*", "would answer any Host header"),
        ("shop.example.com:8080", "is no host"),
        ("evil host", "is no host"),
        ("-shop.example.com", "is no host"),
        ("shop.*.example.com", "is no host"),
    ],
)
def test_allowed_hosts_refuse_what_would_trust_any_host(value: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        load(ALLOWED_HOSTS=value, **dev_env())


def test_the_redis_keys_are_prefixed_livecart_by_default() -> None:
    assert load(**dev_env()).redis_key_prefix == "livecart"


@pytest.mark.parametrize("value", ["livecart", "livecart:staging", "lc_2-b", "test:0af3"])
def test_a_redis_key_prefix_is_words_joined_by_colons(value: str) -> None:
    assert load(**dev_env(), REDIS_KEY_PREFIX=value).redis_key_prefix == value


@pytest.mark.parametrize("value", ["", "live cart", "livecart:", ":livecart", "a::b", "x" * 65])
def test_a_redis_key_prefix_refuses_what_would_blur_the_key_segments(value: str) -> None:
    with pytest.raises(ValidationError, match="redis_key_prefix"):
        load(**dev_env(), REDIS_KEY_PREFIX=value)


def test_the_signing_keys_are_secrets() -> None:
    settings = load(**dev_env())

    assert settings.jwt_secret == TEST_JWT_SECRET
    assert settings.order_token_key == TEST_ORDER_TOKEN_KEY
    assert TEST_JWT_SECRET.get_secret_value() not in repr(settings)
    assert TEST_ORDER_TOKEN_KEY.get_secret_value() not in repr(settings)


@pytest.mark.parametrize("name", ["JWT_SECRET", "ORDER_TOKEN_KEY"])
def test_each_signing_key_is_required(name: str) -> None:
    env = dev_env()
    del env[name]

    with pytest.raises(ValidationError, match=name.lower()):
        load(**env)


@pytest.mark.parametrize("name", ["JWT_SECRET", "ORDER_TOKEN_KEY"])
def test_a_signing_key_needs_32_bytes(name: str) -> None:
    short = "s3cr3t-" * 4 + "abc"  # 31 bytes

    with pytest.raises(ValidationError, match="at least 32 bytes long") as excinfo:
        load(**{**dev_env(), name: short})

    assert name.lower() in str(excinfo.value)
    assert short not in str(excinfo.value)
    # Bytes, not characters: 16 two-byte characters are enough.
    assert load(**{**dev_env(), name: "é" * 16})


def test_the_order_token_key_differs_from_the_jwt_secret() -> None:
    shared = TEST_JWT_SECRET.get_secret_value()

    with pytest.raises(ValidationError, match="ORDER_TOKEN_KEY must differ from JWT_SECRET") as exc:
        load(**{**dev_env(), "ORDER_TOKEN_KEY": shared})

    assert shared not in str(exc.value)


def test_placeholder_signing_keys_are_rejected_in_production() -> None:
    env = {
        **dev_env(password="p4ss", redis_password="p4ss"),
        "JWT_SECRET": "change-me-jwt-change-me-jwt-change-me-jwt",
        "ORDER_TOKEN_KEY": "change-me-order-change-me-order-change-me",
    }

    assert load(**env)  # accepted in development
    with pytest.raises(ValidationError) as excinfo:
        load(APP_ENV="production", **env)

    message = str(excinfo.value)
    assert "JWT_SECRET" in message
    assert "ORDER_TOKEN_KEY" in message
    assert "change-me-jwt" not in message


WEBHOOK_SECRET = "simulator-webhook-" * 2
WEBHOOK_SECRET_NAMES = [name.upper() for name in COMMENT_WEBHOOK_SECRETS.values()]


def test_the_api_serves_the_simulator_webhook_only_by_default() -> None:
    settings = load(**dev_env())

    assert settings.enabled_platforms == (Platform.SIMULATOR,)
    assert all(settings.comment_webhook_secret(platform) is None for platform in Platform)


def test_enabled_platforms_are_listed_between_commas_each_once() -> None:
    settings = load(ENABLED_PLATFORMS=" Simulator, tiktok,simulator,", **dev_env())

    assert settings.enabled_platforms == (Platform.SIMULATOR, Platform.TIKTOK)


@pytest.mark.parametrize(
    ("value", "message"),
    [("", "at least one platform"), (" , ", "at least one platform"), ("myspace", "enum")],
)
def test_enabled_platforms_refuse_an_empty_list_and_unknown_platforms(
    value: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message) as excinfo:
        load(ENABLED_PLATFORMS=value, **dev_env())

    assert "enabled_platforms" in str(excinfo.value)


@pytest.mark.parametrize("platform", list(Platform))
def test_each_platform_has_its_own_webhook_secret(platform: Platform) -> None:
    settings = load(**{f"COMMENT_WEBHOOK_SECRET_{platform.upper()}": WEBHOOK_SECRET}, **dev_env())

    secret = settings.comment_webhook_secret(platform)
    assert secret is not None
    assert secret.get_secret_value() == WEBHOOK_SECRET
    assert WEBHOOK_SECRET not in repr(settings)
    others = [other for other in Platform if other is not platform]
    assert all(settings.comment_webhook_secret(other) is None for other in others)


@pytest.mark.parametrize("name", WEBHOOK_SECRET_NAMES)
def test_a_webhook_secret_needs_32_bytes(name: str) -> None:
    short = "s3cr3t-" * 4 + "abc"  # 31 bytes

    with pytest.raises(ValidationError, match="at least 32 bytes long") as excinfo:
        load(**{**dev_env(), name: short})

    assert name.lower() in str(excinfo.value)
    assert short not in str(excinfo.value)


def test_placeholder_webhook_secrets_are_rejected_in_production() -> None:
    env = {
        **dev_env(password="p4ss", redis_password="p4ss"),
        "COMMENT_WEBHOOK_SECRET_SIMULATOR": "change-me-simulator-change-me-simulator",
    }

    assert load(**env)  # accepted in development
    with pytest.raises(ValidationError) as excinfo:
        load(APP_ENV="production", **env)

    message = str(excinfo.value)
    assert "COMMENT_WEBHOOK_SECRET_SIMULATOR" in message
    assert "change-me-simulator" not in message


def test_the_queue_holds_20000_envelopes_of_5_kib_in_256_mib_by_default() -> None:
    settings = load(**dev_env())

    assert settings.queue_max_depth == 20_000
    assert settings.queue_max_envelope_bytes == 5_120
    assert settings.redis_maxmemory_bytes == 256 * 1024 * 1024
    # 20000 x 5120 x 1.25: 47.7 % of maxmemory, within the half the queue may take.
    assert settings.queue_memory_bytes == 128_000_000
    assert settings.queue_memory_bytes <= settings.redis_maxmemory_bytes // 2


@pytest.mark.parametrize(
    ("name", "refused", "message"),
    [
        ("QUEUE_MAX_DEPTH", "0", "from 1 to 1000000 comments"),
        ("QUEUE_MAX_DEPTH", "1000001", "from 1 to 1000000 comments"),
        ("QUEUE_MAX_ENVELOPE_BYTES", "4096", "from 5120 to 65536 bytes"),
        ("QUEUE_MAX_ENVELOPE_BYTES", "65537", "from 5120 to 65536 bytes"),
        ("REDIS_MAXMEMORY_BYTES", "1048575", "at least 1048576 bytes"),
    ],
)
def test_the_queue_settings_stay_within_their_bounds(name: str, refused: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        load(**{name: refused}, **dev_env())


def test_a_full_queue_must_fit_in_half_of_redis() -> None:
    # 20000 x 5120 x 1.25 = 128000000 bytes: exactly half of 256000000.
    assert load(REDIS_MAXMEMORY_BYTES="256000000", **dev_env())

    with pytest.raises(ValidationError, match="within half of REDIS_MAXMEMORY_BYTES"):
        load(REDIS_MAXMEMORY_BYTES="255999999", **dev_env())
    with pytest.raises(ValidationError, match="within half of REDIS_MAXMEMORY_BYTES"):
        load(QUEUE_MAX_DEPTH="21000", **dev_env())
    with pytest.raises(ValidationError, match="within half of REDIS_MAXMEMORY_BYTES"):
        load(QUEUE_MAX_ENVELOPE_BYTES="5400", **dev_env())
    # A larger Redis takes a deeper queue.
    deeper = load(
        QUEUE_MAX_DEPTH="40000", REDIS_MAXMEMORY_BYTES=str(512 * 1024 * 1024), **dev_env()
    )
    assert deeper.queue_max_depth == 40_000


def test_the_worker_runs_16_handlers_on_a_pool_of_22_connections_by_default() -> None:
    settings = load(**dev_env())

    assert (settings.worker_concurrency, settings.worker_batch) == (16, 32)
    assert settings.message_process_timeout == timedelta(seconds=30)
    assert settings.worker_db_max_connections == 22
    assert (settings.worker_pool.size, settings.worker_pool.max_overflow) == (20, 2)


@pytest.mark.parametrize(
    ("name", "refused", "message"),
    [
        ("WORKER_CONCURRENCY", "0", "from 1 to 64 handlers"),
        ("WORKER_CONCURRENCY", "65", "from 1 to 64 handlers"),
        ("WORKER_BATCH", "0", "from 1 to 256 deliveries"),
        ("WORKER_BATCH", "257", "from 1 to 256 deliveries"),
        ("MESSAGE_PROCESS_TIMEOUT_S", "4", "from 5 to 300 seconds"),
        ("MESSAGE_PROCESS_TIMEOUT_S", "301", "from 5 to 300 seconds"),
        ("WORKER_DB_MAX_CONNECTIONS", "0", "from 1 to 97 connections"),
        ("WORKER_DB_MAX_CONNECTIONS", "98", "from 1 to 97 connections"),
    ],
)
def test_the_worker_settings_stay_within_their_bounds(
    name: str, refused: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        load(**{name: refused}, **dev_env())


def test_the_worker_pool_must_fit_its_connection_cap() -> None:
    with pytest.raises(ValidationError, match="needs a pool of 23 connections"):
        load(WORKER_CONCURRENCY="17", **dev_env())
    with pytest.raises(ValidationError, match="above WORKER_DB_MAX_CONNECTIONS=21"):
        load(WORKER_DB_MAX_CONNECTIONS="21", **dev_env())

    wider = load(WORKER_CONCURRENCY="17", WORKER_DB_MAX_CONNECTIONS="23", **dev_env())
    fewer = load(WORKER_CONCURRENCY="4", **dev_env())

    assert wider.worker_pool.max_connections == 23
    assert fewer.worker_pool.size == 8


def test_a_comment_is_reclaimed_after_120_s_and_dead_lettered_after_3_deliveries() -> None:
    settings = load(**dev_env())

    assert settings.queue_max_deliveries == 3
    assert settings.longest_pending == timedelta(seconds=60)
    assert settings.queue_reclaim_min_idle == timedelta(seconds=120)


@pytest.mark.parametrize(
    ("env", "idle"),
    [
        # A window of 16 for 16 handlers: one deadline, so 60 s.
        ({"WORKER_BATCH": "16"}, timedelta(seconds=60)),
        # Three rounds of 10 s deadlines.
        ({"WORKER_BATCH": "40", "MESSAGE_PROCESS_TIMEOUT_S": "10"}, timedelta(seconds=60)),
        ({"WORKER_BATCH": "8", "WORKER_CONCURRENCY": "4"}, timedelta(seconds=120)),
        # Longer than derived is allowed, shorter is not.
        ({"QUEUE_RECLAIM_MIN_IDLE_MS": "300000"}, timedelta(seconds=300)),
        # Empty, as compose passes it when .env leaves it unset: derived.
        ({"QUEUE_RECLAIM_MIN_IDLE_MS": ""}, timedelta(seconds=120)),
        ({"QUEUE_RECLAIM_MIN_IDLE_MS": "120000"}, timedelta(seconds=120)),
    ],
)
def test_the_reclaim_idle_time_derives_from_the_worker_window(
    env: dict[str, str], idle: timedelta
) -> None:
    assert load(**env, **dev_env()).queue_reclaim_min_idle == idle


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"QUEUE_RECLAIM_MIN_IDLE_MS": "119999"}, "from 120000"),
        ({"QUEUE_RECLAIM_MIN_IDLE_MS": "3600001"}, "to 3600000 ms"),
        # A wider window holds its deliveries longer: 2 x 4 x 30 s.
        ({"QUEUE_RECLAIM_MIN_IDLE_MS": "200000", "WORKER_BATCH": "64"}, "from 240000"),
        ({"QUEUE_MAX_DELIVERIES": "0"}, "from 1 to 10 deliveries"),
        ({"QUEUE_MAX_DELIVERIES": "11"}, "from 1 to 10 deliveries"),
    ],
)
def test_the_reclaim_settings_are_refused_outside_their_bounds(
    env: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        load(**env, **dev_env())


def test_the_buyer_links_open_the_demo_app_by_default() -> None:
    assert load(**dev_env()).buyer_link_scheme == "livecart"
    assert load(BUYER_LINK_SCHEME="samplelive", **dev_env()).buyer_link_scheme == "samplelive"


@pytest.mark.parametrize("scheme", ["", "Livecart", "1app", "live cart", "https://x"])
def test_a_buyer_link_scheme_is_a_uri_scheme(scheme: str) -> None:
    with pytest.raises(ValidationError, match="must be a URI scheme"):
        load(BUYER_LINK_SCHEME=scheme, **dev_env())


MIGRATOR_URL = "postgresql+asyncpg://livecart_migrator:{password}@localhost:55432/livecart"


def load_migration(**env: str) -> MigrationSettings:
    with pytest.MonkeyPatch.context() as patch:
        for name, value in env.items():
            patch.setenv(name, value)
        return MigrationSettings(_env_file=None)


def test_migration_settings_need_only_the_migrator_url() -> None:
    settings = load_migration(MIGRATOR_URL=MIGRATOR_URL.format(password="change-me-migrator"))

    assert "livecart_migrator" in settings.migrator_url.get_secret_value()
    assert not hasattr(settings, "database_url")


def test_migrator_placeholder_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="MIGRATOR_URL"):
        load_migration(
            APP_ENV="production", MIGRATOR_URL=MIGRATOR_URL.format(password="change-me-migrator")
        )


def test_migrator_url_must_use_asyncpg() -> None:
    with pytest.raises(ValidationError, match=r"postgresql\+asyncpg"):
        load_migration(MIGRATOR_URL="postgresql://livecart_migrator:x@localhost/livecart")


def _seed_settings(**env: str) -> SeedSettings:
    with pytest.MonkeyPatch.context() as patch:
        for name, value in env.items():
            patch.setenv(name, value)
        return SeedSettings(_env_file=None)


SEED_ENV = {
    "MIGRATOR_URL": "postgresql+asyncpg://livecart_migrator:change-me-migrator@localhost/livecart",
    "SEED_DEMO_PASSWORD": "change-me-demo-seller",
}


def test_the_metrics_are_served_on_the_internal_port_9100_by_default() -> None:
    assert load(**dev_env()).metrics_port == 9100
    assert load(**dev_env(), METRICS_PORT="9200").metrics_port == 9200


def test_an_empty_metrics_port_serves_no_metrics() -> None:
    assert load(**dev_env(), METRICS_PORT="").metrics_port is None


@pytest.mark.parametrize("value", ["0", "65536", "-1"])
def test_the_metrics_port_is_a_tcp_port(value: str) -> None:
    with pytest.raises(ValidationError, match="TCP port from 1 to 65535"):
        load(**dev_env(), METRICS_PORT=value)


def test_seed_settings_need_the_migrator_and_the_demo_password() -> None:
    settings = _seed_settings(**SEED_ENV)

    assert settings.seed_demo_password.get_secret_value() == "change-me-demo-seller"
    assert "change-me-demo-seller" not in repr(settings)
    assert settings.default_currency == "EUR"
    for missing in SEED_ENV:
        with pytest.raises(ValidationError, match=missing.lower()):
            _seed_settings(**{name: value for name, value in SEED_ENV.items() if name != missing})


def test_seed_placeholders_are_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="replace: MIGRATOR_URL, SEED_DEMO_PASSWORD"):
        _seed_settings(APP_ENV="production", **SEED_ENV)


def test_seed_settings_check_the_migrator_url_and_the_currency() -> None:
    with pytest.raises(ValidationError, match=r"postgresql\+asyncpg"):
        _seed_settings(**{**SEED_ENV, "MIGRATOR_URL": "postgresql://u:p@localhost/livecart"})
    with pytest.raises(ValidationError, match="ISO 4217"):
        _seed_settings(DEFAULT_CURRENCY="euro", **SEED_ENV)
