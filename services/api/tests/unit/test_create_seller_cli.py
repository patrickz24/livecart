"""``livecart-create-seller`` without a database: arguments, password sources, exit codes."""

import io
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from livecart_api.cli import create_seller as cli
from livecart_core.application.dto import SellerRecord
from livecart_core.application.ports import SellerConflict, ServiceUnavailable

PASSWORD = "correct horse battery staple"
ARGS = ["--email", "seller@example.test", "--slug", "my-shop", "--display-name", "My shop"]


def _keep_logging(*, service: str, level: str = "INFO", stream: object = None) -> None:
    """In place of ``configure_logging``: main() would reroute the whole test session's logs."""


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings and password from the environment only, and the session's logging kept."""
    monkeypatch.chdir(tmp_path)  # no .env to read
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://livecart_app:unit@db.invalid/x")
    monkeypatch.setenv(cli.PASSWORD_VARIABLE, PASSWORD)
    monkeypatch.setattr(cli, "configure_logging", _keep_logging)


def _prompt(*answers: str) -> Callable[[str], str]:
    replies = iter(answers)

    def prompt(_: str) -> str:
        return next(replies)

    return prompt


def test_the_three_fields_are_required() -> None:
    args = cli.parse_args(ARGS)

    assert (args.email, args.slug, args.display_name) == (
        "seller@example.test",
        "my-shop",
        "My shop",
    )
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args(ARGS[:4])
    assert excinfo.value.code == 2


def test_a_password_argument_does_not_exist(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args([*ARGS, "--password", PASSWORD])

    assert excinfo.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_the_environment_variable_gives_the_password_without_a_prompt() -> None:
    password = cli.read_password(
        {cli.PASSWORD_VARIABLE: PASSWORD}, interactive=True, prompt=_prompt()
    )

    assert password == PASSWORD


def test_without_the_variable_the_password_is_typed_twice() -> None:
    password = cli.read_password(
        {cli.PASSWORD_VARIABLE: ""}, interactive=True, prompt=_prompt(PASSWORD, PASSWORD)
    )

    assert password == PASSWORD


def test_two_different_passwords_are_refused() -> None:
    with pytest.raises(cli.PasswordUnavailable, match="differ"):
        cli.read_password({}, interactive=True, prompt=_prompt(PASSWORD, PASSWORD + "!"))


def test_without_the_variable_nor_a_terminal_nothing_is_read() -> None:
    with pytest.raises(cli.PasswordUnavailable, match=cli.PASSWORD_VARIABLE):
        cli.read_password({}, interactive=False, prompt=_prompt())


@pytest.fixture
def command_line(environment: None, monkeypatch: pytest.MonkeyPatch) -> list[SellerRecord]:
    """``main`` around a recording ``create_seller``: the sellers it was asked to create."""
    created: list[SellerRecord] = []

    async def create_seller(
        settings: cli.CreateSellerSettings,
        *,
        email: str,
        slug: str,
        display_name: str,
        password: str,
    ) -> SellerRecord:
        assert password == PASSWORD
        seller = SellerRecord(
            UUID(int=7), email, slug, display_name, "hash", datetime(2026, 9, 26, tzinfo=UTC)
        )
        created.append(seller)
        return seller

    monkeypatch.setattr(cli, "create_seller", create_seller)
    return created


def test_main_prints_the_new_seller(
    command_line: list[SellerRecord], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(ARGS) == 0

    (seller,) = command_line
    out = capsys.readouterr().out
    assert out == f"created seller {seller.id} (my-shop)\n"
    assert PASSWORD not in out


def test_a_bad_field_is_refused_before_the_password_is_asked(
    command_line: list[SellerRecord],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(cli.PASSWORD_VARIABLE)
    monkeypatch.setattr(sys, "stdin", io.StringIO())

    assert cli.main([*ARGS[:2], "--slug", "My Shop", *ARGS[4:]]) == 1

    assert "is not a slug" in capsys.readouterr().err
    assert command_line == []


def test_no_password_and_no_terminal_exits_with_1(
    command_line: list[SellerRecord],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(cli.PASSWORD_VARIABLE)
    monkeypatch.setattr(sys, "stdin", io.StringIO())

    assert cli.main(ARGS) == 1

    assert cli.PASSWORD_VARIABLE in capsys.readouterr().err
    assert command_line == []


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (SellerConflict("email"), "another seller has this email"),
        (ServiceUnavailable("database unavailable: OSError"), "database unavailable"),
    ],
)
def test_a_conflict_or_an_outage_exits_with_1(
    environment: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    message: str,
) -> None:
    async def failing(settings: cli.CreateSellerSettings, **_: str) -> SellerRecord:
        raise error

    monkeypatch.setattr(cli, "create_seller", failing)

    assert cli.main(ARGS) == 1
    err = capsys.readouterr().err
    assert err == f"livecart-create-seller: {error}\n"
    assert message in err


def test_the_settings_need_a_postgresql_asyncpg_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "mysql://root@localhost/livecart")

    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        cli.CreateSellerSettings()
