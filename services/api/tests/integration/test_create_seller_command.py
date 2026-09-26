"""``livecart-create-seller`` run as a process against livecart_test, as an operator runs it.

The password always comes from the environment here. Without it the command
would prompt: on Windows, getpass reads the console itself, and the NUL device
counts as a terminal, so the run would wait for a keyboard.
``tests/unit/test_create_seller_cli.py`` covers the prompt and its refusal.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_api.cli.create_seller import PASSWORD_VARIABLE
from livecart_core.infrastructure.db.sellers import SqlSellerRepository
from livecart_core.infrastructure.passwords import Argon2PasswordHasher
from livecart_core.testing.database import IntegrationSettings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
ARGS = ("--email", "Seller@Example.test", "--slug", "my-shop", "--display-name", "My shop")


async def _run(args: tuple[str, ...], *, database_url: str, cwd: Path) -> tuple[int, str, str]:
    """Exit code, standard output and standard error of one run, with newlines as LF."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in ("DATABASE_URL", PASSWORD_VARIABLE)
    }
    environment["DATABASE_URL"] = database_url
    environment[PASSWORD_VARIABLE] = PASSWORD
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "livecart_api.cli.create_seller",
        *args,
        cwd=cwd,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), timeout=60)
    assert process.returncode is not None
    return process.returncode, _text(out), _text(err)


def _text(stream: bytes) -> str:
    # Text-mode streams write CRLF on Windows.
    return stream.decode().replace("\r\n", "\n")


async def test_the_command_creates_a_seller_who_can_log_in(
    clean_database: None,
    app_engine: AsyncEngine,
    integration_settings: IntegrationSettings,
    tmp_path: Path,
) -> None:
    database_url = integration_settings.database_url.get_secret_value()

    code, out, err = await _run(ARGS, database_url=database_url, cwd=tmp_path)

    assert (code, err) == (0, "")
    seller = await SqlSellerRepository(app_engine).find_by_email("seller@example.test")
    assert seller is not None
    assert out == f"created seller {seller.id} (my-shop)\n"
    assert (seller.email, seller.display_name) == ("Seller@Example.test", "My shop")
    assert seller.password_hash.startswith("$argon2id$")
    assert await Argon2PasswordHasher().verify(seller.password_hash, PASSWORD) is True
    assert PASSWORD not in out + err


async def test_a_second_account_with_the_email_in_another_case_is_refused(
    clean_database: None,
    app_engine: AsyncEngine,
    integration_settings: IntegrationSettings,
    tmp_path: Path,
) -> None:
    database_url = integration_settings.database_url.get_secret_value()
    await _run(ARGS, database_url=database_url, cwd=tmp_path)
    again = ("--email", "SELLER@example.TEST", "--slug", "other-shop", "--display-name", "Other")

    code, out, err = await _run(again, database_url=database_url, cwd=tmp_path)

    assert (code, out) == (1, "")
    assert err == "livecart-create-seller: another seller has this email\n"
    assert await SqlSellerRepository(app_engine).find_by_email("seller@example.test") is not None
