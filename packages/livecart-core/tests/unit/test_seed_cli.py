"""``livecart-seed`` without a database: its arguments, its summary and its refusals."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from livecart_core import seed as cli
from livecart_core.application.demo_seed import Seeded, SeededSeller, SeededSession, SeedReport
from livecart_core.application.dto import LiveSessionRecord, Platform, SellerRecord
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.infrastructure.db.session_reset import SessionReset
from livecart_core.seed import main, parse_args, summary

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _keep_logging(**_: object) -> None:
    """In place of ``configure_logging``: main() would reroute the whole test session's logs."""


def _seller(slug: str) -> SellerRecord:
    return SellerRecord(
        id=UUID(int=1),
        email=f"{slug}@livecart.test",
        slug=slug,
        display_name=slug,
        password_hash="!",
        created_at=NOW,
    )


def _session(live_id: str) -> LiveSessionRecord:
    return LiveSessionRecord(
        id=UUID(int=2),
        seller_id=UUID(int=1),
        title=live_id,
        platform=Platform.SIMULATOR,
        external_live_id=live_id,
        status=LiveSessionStatus.LIVE,
        created_at=NOW,
    )


def test_the_reset_is_asked_for_explicitly() -> None:
    assert parse_args([]).reset_loadtest is False
    assert parse_args(["--reset-loadtest"]).reset_loadtest is True


def test_an_unknown_argument_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_args(["--reset-everything"])

    assert excinfo.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_the_summary_says_what_each_record_was() -> None:
    report = SeedReport(
        (
            SeededSeller(
                _seller("demo-seller"),
                Seeded.CREATED,
                (SeededSession(_session("loadtest"), Seeded.FOUND, 0),),
            ),
        )
    )

    assert summary(report, None) == [
        "seller demo-seller: created",
        "session loadtest: found, 0 products added, live",
    ]
    assert summary(report, SessionReset(comments=5, intents=4, orders=3))[-1] == (
        "session loadtest reset: 5 comments, 4 intents and 3 orders deleted, stock restored"
    )


def test_an_unreachable_database_exits_with_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No .env in the working directory, and a closed port: nothing real is ever touched.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MIGRATOR_URL", "postgresql+asyncpg://livecart_migrator:x@127.0.0.1:1/x")
    monkeypatch.setenv("SEED_DEMO_PASSWORD", "demo password of the tests")
    monkeypatch.setattr(cli, "configure_logging", _keep_logging)

    code = main([])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("livecart-seed: database unavailable")
    assert "demo password" not in captured.err
