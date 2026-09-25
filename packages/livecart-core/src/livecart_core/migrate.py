"""``livecart-migrate``: the Alembic command line, bound to the migrations of this package.

Without arguments it runs ``upgrade head``, which is what the migrate container
does. Any other Alembic command works the same way, for example
``livecart-migrate current``, ``livecart-migrate downgrade base`` or
``livecart-migrate upgrade head --sql`` (prints the SQL, connects to nothing).

The migrations ship inside the package (``script_location`` is a package
resource), so the command needs no alembic.ini and works from any directory.
Logs go to stderr, keeping stdout for command output such as the SQL.
"""

import sys
from argparse import Namespace
from collections.abc import Sequence

from alembic.config import CommandLine, Config
from pydantic import SecretStr

from livecart_core.logging import configure_logging
from livecart_core.settings import MigrationSettings

PROG = "livecart-migrate"
SCRIPT_LOCATION = "livecart_core:migrations"
DEFAULT_COMMAND = ("upgrade", "head")


def build_config(database_url: SecretStr, *, cmd_opts: Namespace | None = None) -> Config:
    """Alembic configuration pointing at the packaged migrations and ``database_url``."""
    config = Config(cmd_opts=cmd_opts)
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.set_main_option("path_separator", "os")
    # Read by env.py; attributes bypass the ini interpolation that '%' would break.
    config.attributes["database_url"] = database_url.get_secret_value()
    return config


def parse_args(argv: Sequence[str]) -> tuple[CommandLine, Namespace]:
    cli = CommandLine(prog=PROG)
    options = cli.parser.parse_args(list(argv) or list(DEFAULT_COMMAND))
    if not hasattr(options, "cmd"):
        cli.parser.error("an Alembic command is required, for example: upgrade head")
    return cli, options


def execute(cli: CommandLine, options: Namespace, database_url: SecretStr) -> None:
    cli.run_cmd(build_config(database_url, cmd_opts=options), options)


def main(argv: Sequence[str] | None = None) -> None:
    # Parse first, so --help works without any configuration.
    cli, options = parse_args(sys.argv[1:] if argv is None else argv)
    settings = MigrationSettings()
    configure_logging(service="migrate", level=settings.log_level, stream=sys.stderr)
    execute(cli, options, settings.migrator_url)


if __name__ == "__main__":
    main()
