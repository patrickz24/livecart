from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from livecart_core.healthcheck import DEFAULT_HEARTBEAT_FILE
from livecart_core.settings import AppEnv, LogLevel, MigrationSettings, Settings

APP_URL = "postgresql+asyncpg://livecart_app:{password}@localhost:55432/livecart"
REDIS_URL = "redis://:{password}@localhost:56379/0"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from an environment without any LiveCart variable."""
    names = ("APP_ENV", "LOG_LEVEL", "DATABASE_URL", "REDIS_URL", "MIGRATOR_URL")
    for name in (*names, "WORKER_HEARTBEAT_FILE"):
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
    }


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
        load(DATABASE_URL=database_url, REDIS_URL=REDIS_URL.format(password="x"))

    assert "postgresql+asyncpg" in str(excinfo.value)
    assert "s3cr3t" not in str(excinfo.value)


def test_redis_url_must_use_a_redis_scheme() -> None:
    with pytest.raises(ValidationError, match="redis or rediss"):
        load(DATABASE_URL=APP_URL.format(password="x"), REDIS_URL="http://localhost:6379")


def test_tls_redis_is_accepted() -> None:
    settings = load(DATABASE_URL=APP_URL.format(password="x"), REDIS_URL="rediss://:x@cache:6380/0")

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
        "SOME_OTHER_PROCESS_VARIABLE=ignored\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.log_level is LogLevel.WARNING
    assert "from-file" in settings.database_url.get_secret_value()


def test_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DATABASE_URL={APP_URL.format(password='x')}\nREDIS_URL=redis://file:6379/0\n",
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
