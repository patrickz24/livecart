import socket
from pathlib import Path

import pytest
from pydantic import SecretStr

from livecart_core.testing.database import (
    IntegrationSettings,
    IntegrationSettingsError,
    UnsafeTestDatabaseError,
    ensure_test_database,
    load_integration_settings,
    unreachable_servers,
)

BASE = "postgresql+asyncpg://livecart_app:s3cr3t@localhost:55432"


def test_a_database_ending_in_test_is_accepted() -> None:
    url = SecretStr(f"{BASE}/livecart_test")

    assert ensure_test_database(url, variable="TEST_DATABASE_URL") == "livecart_test"


@pytest.mark.parametrize("database", ["livecart", "livecart_test_backup", "test", "livecart-test"])
def test_any_other_database_is_refused(database: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError) as excinfo:
        ensure_test_database(SecretStr(f"{BASE}/{database}"), variable="TEST_DATABASE_URL")

    message = str(excinfo.value)
    assert "TEST_DATABASE_URL" in message
    assert repr(database) in message
    assert "s3cr3t" not in message


def test_a_url_without_database_is_refused() -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        ensure_test_database(SecretStr(BASE), variable="TEST_MIGRATOR_URL")


@pytest.fixture
def test_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("TEST_DATABASE_URL", f"{BASE}/livecart_test")
    monkeypatch.setenv("TEST_MIGRATOR_URL", f"{BASE}/livecart_test")
    monkeypatch.setenv("TEST_REDIS_URL", "redis://:s3cr3t@localhost:56379/15")
    return monkeypatch


def test_the_fixture_settings_load_when_every_url_is_safe(test_env: pytest.MonkeyPatch) -> None:
    settings = load_integration_settings(env_file=None)

    assert settings.redis_url.get_secret_value().endswith("/15")


@pytest.mark.parametrize("variable", ["TEST_DATABASE_URL", "TEST_MIGRATOR_URL"])
def test_the_fixture_settings_refuse_a_non_test_database(
    test_env: pytest.MonkeyPatch, variable: str
) -> None:
    test_env.setenv(variable, f"{BASE}/livecart")

    with pytest.raises(UnsafeTestDatabaseError, match=variable):
        load_integration_settings(env_file=None)


def test_missing_variables_explain_how_to_run_or_skip(
    test_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    test_env.delenv("TEST_REDIS_URL")

    with pytest.raises(IntegrationSettingsError) as excinfo:
        load_integration_settings(env_file=tmp_path / "missing.env")

    message = str(excinfo.value)
    assert "-m 'not integration'" in message
    assert "redis_url" in message
    assert "s3cr3t" not in message


def test_unreachable_servers_are_reported_once_each() -> None:
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        open_port = listening.getsockname()[1]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]  # bound but not listening
            settings = IntegrationSettings(
                database_url=SecretStr(f"postgresql+asyncpg://u:p@127.0.0.1:{open_port}/x_test"),
                migrator_url=SecretStr(f"postgresql+asyncpg://m:p@127.0.0.1:{open_port}/x_test"),
                redis_url=SecretStr(f"redis://:p@127.0.0.1:{closed_port}/15"),
                _env_file=None,
            )

            down = unreachable_servers(settings, timeout_s=0.5)

    assert down == [f"127.0.0.1:{closed_port}"]
