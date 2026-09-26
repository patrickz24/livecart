"""``livecart-seed``: load the demo data (``application.demo_seed``), idempotently.

Usage, from the repository root (``.env`` gives ``MIGRATOR_URL`` and
``SEED_DEMO_PASSWORD``), or as the one-shot ``seed`` service of compose,
which runs after ``migrate``::

    uv run livecart-seed                   # create what is missing
    uv run livecart-seed --reset-loadtest  # then empty the load test session

It connects as ``livecart_migrator``: ``--reset-loadtest`` deletes the
comments, intents, orders, payments and shipments of the ``loadtest`` session
and puts its stock back to 200 (``infrastructure.db.session_reset``), which
``livecart_app`` has no privilege for. Every run creates only what is
missing, so running it again changes nothing.

Standard output gets one line per seller and session, and the reset's
counts; logs go to standard error. A conflict (a demo live id held by
another seller, or a reset of an ended load test session), a refused value
or an unreachable database exits with 1 and a message on standard error, a
usage error with 2.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Final

from livecart_core.application.demo_seed import (
    LOADTEST,
    LOADTEST_LIVE_ID,
    SeedConflict,
    SeedDemo,
    SeedReport,
)
from livecart_core.application.ports import SellerConflict, ServiceUnavailable
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.infrastructure.clock import SystemClock
from livecart_core.infrastructure.db.engine import create_db_engine
from livecart_core.infrastructure.db.live_sessions import SqlLiveSessionRepository
from livecart_core.infrastructure.db.products import SqlProductStockRepository
from livecart_core.infrastructure.db.sellers import SqlSellerRepository
from livecart_core.infrastructure.db.session_reset import SessionReset, reset_live_session
from livecart_core.infrastructure.passwords import Argon2PasswordHasher
from livecart_core.logging import configure_logging
from livecart_core.settings import SeedSettings

PROG: Final = "livecart-seed"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Load the LiveCart demo data, creating only what is missing.",
    )
    parser.add_argument(
        "--reset-loadtest",
        action="store_true",
        help="then delete what the load test session's comments led to, and restore its stock",
    )
    return parser.parse_args(argv)


async def seed(
    settings: SeedSettings, *, reset_loadtest: bool
) -> tuple[SeedReport, SessionReset | None]:
    """Seed on a short-lived engine of one connection, then reset the load test session if asked."""
    engine = create_db_engine(
        settings.migrator_url, application_name=PROG, pool_size=1, max_overflow=0
    )
    try:
        report = await SeedDemo(
            SqlSellerRepository(engine),
            SqlLiveSessionRepository(engine),
            SqlProductStockRepository(engine, currency=settings.default_currency),
            Argon2PasswordHasher(),
            SystemClock(),
        )(password=settings.seed_demo_password.get_secret_value())
        reset = None
        if reset_loadtest:
            loadtest = report.session(LOADTEST_LIVE_ID)
            if loadtest.status is not LiveSessionStatus.LIVE:
                # Its comments would all be rejected as session_not_live.
                raise SeedConflict(f"the session {LOADTEST_LIVE_ID} has ended: make reset")
            reset = await reset_live_session(
                engine,
                loadtest.id,
                {
                    (ProductCode(product.code), Variant(product.variant)): product.stock_total
                    for product in LOADTEST.products
                },
            )
        return report, reset
    finally:
        await engine.dispose()


def summary(report: SeedReport, reset: SessionReset | None) -> list[str]:
    """The lines standard output gets: what each record was, and what the reset deleted."""
    lines = []
    for seeded in report.sellers:
        lines.append(f"seller {seeded.seller.slug}: {seeded.seeded}")
        lines.extend(
            f"session {session.session.external_live_id}: {session.seeded}, "
            f"{session.products_added} products added, {session.session.status}"
            for session in seeded.sessions
        )
    if reset is not None:
        lines.append(
            f"session {LOADTEST_LIVE_ID} reset: {reset.comments} comments, {reset.intents} "
            f"intents and {reset.orders} orders deleted, stock restored"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    # Parse first, so --help works without any configuration.
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = SeedSettings()
    configure_logging(service="seed", level=settings.log_level, stream=sys.stderr)
    try:
        report, reset = asyncio.run(seed(settings, reset_loadtest=args.reset_loadtest))
    except (ValueError, SeedConflict, SellerConflict, ServiceUnavailable) as error:
        sys.stderr.write(f"{PROG}: {error}\n")
        return 1
    sys.stdout.write("".join(f"{line}\n" for line in summary(report, reset)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
