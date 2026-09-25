from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from pydantic import SecretStr

from livecart_core.migrate import build_config, execute, main, parse_args

MIGRATOR_URL = SecretStr("postgresql+asyncpg://livecart_migrator:p%40ss@db.invalid/livecart_test")


def test_the_packaged_migrations_have_a_single_empty_baseline() -> None:
    scripts = ScriptDirectory.from_config(build_config(MIGRATOR_URL))

    assert Path(scripts.dir).name == "migrations"
    assert scripts.get_heads() == ["0001"]
    baseline = scripts.get_revision("0001")
    assert baseline is not None
    assert baseline.down_revision is None


def test_without_arguments_the_command_upgrades_to_head() -> None:
    _, options = parse_args([])

    assert options.cmd[0].__name__ == "upgrade"
    assert options.revision == "head"


def test_offline_upgrade_renders_the_baseline_sql(capsys: pytest.CaptureFixture[str]) -> None:
    cli, options = parse_args(["upgrade", "head", "--sql"])

    execute(cli, options, MIGRATOR_URL)  # offline mode: no connection is opened

    sql = capsys.readouterr().out
    assert "CREATE TABLE alembic_version" in sql
    assert "INSERT INTO alembic_version (version_num) VALUES ('0001')" in sql


def test_offline_downgrade_renders_the_reverse_sql(capsys: pytest.CaptureFixture[str]) -> None:
    cli, options = parse_args(["downgrade", "0001:base", "--sql"])

    execute(cli, options, MIGRATOR_URL)

    assert "DELETE FROM alembic_version WHERE alembic_version.version_num = '0001'" in (
        capsys.readouterr().out
    )


def test_help_needs_no_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIGRATOR_URL", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
