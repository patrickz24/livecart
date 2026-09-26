"""``livecart-create-seller``: open a seller account, since there is no public sign-up.

Usage, from the repository root (``.env`` gives ``DATABASE_URL``)::

    uv run livecart-create-seller --email seller@example.test --slug my-shop \
        --display-name "My shop"

The password is never an argument: a command line stays in the shell's
history and shows in the process list of every user of the machine. It is
read from ``LIVECART_SELLER_PASSWORD`` when that variable is set (the seed,
scripts), and otherwise asked twice on the terminal, without echo. The other
fields are checked before the password is asked for.

The seller is created by ``CreateSeller``, as the demo seed does: the domain
checks every field, the password (12 to 128 characters) is hashed with
argon2id, and an e-mail taken in any case or a taken slug is refused. It
connects as ``livecart_app`` (``DATABASE_URL``), which may insert sellers.
Standard output gets the new seller's id and slug; a refused value, a
conflict or an unreachable database is reported on standard error with exit
code 1, a usage error with exit code 2.
"""

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from pydantic import SecretStr, field_validator

from livecart_core.application.create_seller import CreateSeller
from livecart_core.application.dto import SellerRecord
from livecart_core.application.ports import SellerConflict, ServiceUnavailable
from livecart_core.domain.sellers import check_display_name, check_email, check_slug
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.infrastructure.db.engine import create_db_engine
from livecart_core.infrastructure.db.sellers import SqlSellerRepository
from livecart_core.infrastructure.passwords import Argon2PasswordHasher
from livecart_core.logging import configure_logging
from livecart_core.settings import BaseAppSettings, require_scheme

PROG: Final = "livecart-create-seller"
PASSWORD_VARIABLE: Final = "LIVECART_SELLER_PASSWORD"  # noqa: S105 - a variable name, not a password


class CreateSellerSettings(BaseAppSettings):
    """``DATABASE_URL`` only: creating an account needs no Redis and no signing key."""

    database_url: SecretStr

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        return require_scheme(value, "postgresql+asyncpg")


class PasswordUnavailable(Exception):
    """No password could be read: none in the environment, and no terminal to ask on."""


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Create a LiveCart seller account. The password is read from "
            f"{PASSWORD_VARIABLE}, or asked on the terminal: never pass it as an argument."
        ),
    )
    parser.add_argument("--email", required=True, help="the e-mail the seller logs in with")
    parser.add_argument("--slug", required=True, help="the storefront's URL segment: my-shop")
    parser.add_argument("--display-name", required=True, help="the name the storefront shows")
    return parser.parse_args(argv)


def read_password(
    environ: Mapping[str, str],
    *,
    interactive: bool,
    prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    """The password from ``LIVECART_SELLER_PASSWORD``, or typed twice on the terminal."""
    from_environment = environ.get(PASSWORD_VARIABLE, "")
    if from_environment:
        return from_environment
    if not interactive:
        raise PasswordUnavailable(
            f"set {PASSWORD_VARIABLE}, or run the command in a terminal to type the password"
        )
    password = prompt("Password (12 to 128 characters): ")
    if prompt("Password again: ") != password:
        raise PasswordUnavailable("the two passwords differ")
    return password


async def create_seller(
    settings: CreateSellerSettings, *, email: str, slug: str, display_name: str, password: str
) -> SellerRecord:
    """Create the account through ``CreateSeller`` on its own short-lived engine."""
    engine = create_db_engine(
        settings.database_url,
        application_name=PROG,
        pool_size=1,
        max_overflow=0,
    )
    try:
        create = CreateSeller(SqlSellerRepository(engine), Argon2PasswordHasher(), SystemClock())
        return await create(email=email, slug=slug, display_name=display_name, password=password)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    # Parse first, so --help works without any configuration.
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = CreateSellerSettings()
    configure_logging(service=PROG, level=settings.log_level, stream=sys.stderr)
    try:
        # The fields a typo breaks are refused before the password is asked for.
        email = check_email(args.email)
        slug = check_slug(args.slug)
        display_name = check_display_name(args.display_name)
        password = read_password(os.environ, interactive=sys.stdin.isatty())
        seller = asyncio.run(
            create_seller(
                settings, email=email, slug=slug, display_name=display_name, password=password
            )
        )
    except (ValueError, PasswordUnavailable, SellerConflict, ServiceUnavailable) as error:
        sys.stderr.write(f"{PROG}: {error}\n")
        return 1
    sys.stdout.write(f"created seller {seller.id} ({seller.slug})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
