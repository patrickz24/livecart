from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from pydantic import SecretStr

from livecart_core.infrastructure.db.tables import metadata
from livecart_core.migrate import build_config, execute, main, parse_args

MIGRATOR_URL = SecretStr("postgresql+asyncpg://livecart_migrator:p%40ss@db.invalid/livecart_test")


def test_the_packaged_migrations_form_one_line_from_the_baseline() -> None:
    scripts = ScriptDirectory.from_config(build_config(MIGRATOR_URL))

    assert Path(scripts.dir).name == "migrations"
    assert scripts.get_heads() == ["0006"]
    baseline = scripts.get_revision("0001")
    assert baseline is not None
    assert baseline.down_revision is None
    schema = scripts.get_revision("0002")
    assert schema is not None
    assert schema.down_revision == "0001"
    comments_index = scripts.get_revision("0003")
    assert comments_index is not None
    assert comments_index.down_revision == "0002"
    llm_reason = scripts.get_revision("0004")
    assert llm_reason is not None
    assert llm_reason.down_revision == "0003"
    webhook_lease = scripts.get_revision("0005")
    assert webhook_lease is not None
    assert webhook_lease.down_revision == "0004"
    tracking_lease = scripts.get_revision("0006")
    assert tracking_lease is not None
    assert tracking_lease.down_revision == "0005"


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
    assert "REVOKE ALL ON TABLE alembic_version FROM livecart_app" in sql
    assert (
        "UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001'"
        in sql
    )
    for table in metadata.tables:
        assert f"CREATE TABLE {table} (" in sql
    assert (
        "CREATE UNIQUE INDEX uq_orders_pending_buyer_product ON orders"
        " (session_id, product_id, buyer_platform_user_id) WHERE status = 'pending_payment'"
    ) in sql
    assert "GENERATED ALWAYS AS ((stock_total - reserved) - sold) STORED" in sql
    assert "CREATE INDEX ix_comments_session_id ON comments (session_id)" in sql
    assert (
        "UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0002'"
        in sql
    )
    assert "ALTER TABLE comments ADD COLUMN llm_reason VARCHAR(32)" in sql
    assert "ADD CONSTRAINT ck_comments_llm_reason_when_ok CHECK" in sql
    assert (
        "UPDATE alembic_version SET version_num='0004' WHERE alembic_version.version_num = '0003'"
        in sql
    )
    assert (
        "ALTER TABLE processed_webhook_events ADD COLUMN claimed_until TIMESTAMP WITH TIME ZONE"
        in sql
    )
    assert (
        "UPDATE alembic_version SET version_num='0005' WHERE alembic_version.version_num = '0004'"
        in sql
    )
    assert (
        "UPDATE shipments SET last_event_at=shipments.carrier_created_at"
        " WHERE shipments.last_event_at IS NULL"
    ) in sql
    assert "ALTER TABLE shipments ALTER COLUMN last_event_at SET NOT NULL" in sql
    assert "ALTER TABLE shipments ADD COLUMN tracking_lease_until TIMESTAMP WITH TIME ZONE" in sql
    assert (
        "CREATE INDEX ix_shipments_in_flight ON shipments (id)"
        " WHERE status IN ('created', 'in_transit', 'out_for_delivery')"
    ) in sql
    assert (
        "UPDATE alembic_version SET version_num='0006' WHERE alembic_version.version_num = '0005'"
        in sql
    )


def test_offline_downgrade_renders_the_reverse_sql(capsys: pytest.CaptureFixture[str]) -> None:
    cli, options = parse_args(["downgrade", "0006:base", "--sql"])

    execute(cli, options, MIGRATOR_URL)

    sql = capsys.readouterr().out
    assert sql.index("DROP INDEX ix_shipments_in_flight;") < sql.index(
        "DROP COLUMN tracking_lease_until;"
    )
    assert "ALTER TABLE shipments ALTER COLUMN last_event_at DROP NOT NULL" in sql
    assert sql.index("DROP COLUMN tracking_lease_until;") < sql.index("DROP COLUMN claimed_until;")
    assert sql.index("DROP COLUMN claimed_until;") < sql.index("DROP COLUMN llm_reason;")
    assert sql.index("DROP COLUMN llm_reason;") < sql.index("DROP INDEX ix_comments_session_id;")
    assert sql.index("DROP INDEX ix_comments_session_id;") < sql.index("DROP TABLE comments;")
    for table in metadata.tables:
        assert f"DROP TABLE {table};" in sql
    # Children before parents, so that every foreign key is gone before its target.
    assert sql.index("DROP TABLE shipments;") < sql.index("DROP TABLE orders;")
    assert sql.index("DROP TABLE orders;") < sql.index("DROP TABLE sellers;")
    assert "DELETE FROM alembic_version WHERE alembic_version.version_num = '0001'" in sql


def test_help_needs_no_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIGRATOR_URL", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
