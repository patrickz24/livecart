"""``livecart-seed`` on livecart_test: run twice as a process, then reset the load test session.

The seed connects as the migrator (TEST_MIGRATOR_URL); the orders the reset
must delete are opened by unit 3 as livecart_app, as the worker opens them.
"""

import asyncio
import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from livecart_core.application.demo_seed import SeedConflict
from livecart_core.application.dto import IntentSource, Platform, ReservationRequest
from livecart_core.application.storefront import ViewStorefront
from livecart_core.domain.codes import ProductCode, Variant
from livecart_core.domain.quantity import Quantity
from livecart_core.domain.state_machines import LiveSessionStatus
from livecart_core.infrastructure.db import tables
from livecart_core.infrastructure.db.live_sessions import SqlLiveSessionRepository
from livecart_core.infrastructure.db.products import SqlProductStockRepository
from livecart_core.infrastructure.db.sellers import SqlSellerRepository
from livecart_core.infrastructure.passwords import Argon2PasswordHasher
from livecart_core.seed import seed
from livecart_core.settings import SeedSettings
from livecart_core.testing.backends import PostgresBackend
from livecart_core.testing.database import IntegrationSettings
from livecart_core.testing.fakes import FAKE_CLOCK_START

pytestmark = pytest.mark.integration

PASSWORD = "demo password of the tests"
FIRST_RUN = """\
seller demo-seller: created
session loadtest: created, 1 products added, live
session friday-drop: created, 15 products added, live
seller sample-shop: created
"""
NEXT_RUN = """\
seller demo-seller: found
session loadtest: found, 0 products added, live
session friday-drop: found, 0 products added, live
seller sample-shop: found
"""


async def _run(*args: str, migrator_url: str, cwd: Path) -> tuple[int, str, str]:
    """Exit code, standard output and standard error of one run, with newlines as LF."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in ("MIGRATOR_URL", "SEED_DEMO_PASSWORD")
    }
    environment["MIGRATOR_URL"] = migrator_url
    environment["SEED_DEMO_PASSWORD"] = PASSWORD
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "livecart_core.seed",
        *args,
        cwd=cwd,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), timeout=60)
    assert process.returncode is not None
    return process.returncode, out.decode().replace("\r\n", "\n"), err.decode()


async def _count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    select(func.count()).select_from(tables.metadata.tables[table])
                )
            ).scalar_one()
        )


async def test_the_command_seeds_once_and_finds_everything_the_next_time(
    clean_database: None,
    app_engine: AsyncEngine,
    integration_settings: IntegrationSettings,
    tmp_path: Path,
) -> None:
    migrator_url = integration_settings.migrator_url.get_secret_value()

    first = await _run(migrator_url=migrator_url, cwd=tmp_path)
    counts = [await _count(app_engine, table) for table in ("sellers", "live_sessions", "products")]
    second = await _run(migrator_url=migrator_url, cwd=tmp_path)

    assert first[:2] == (0, FIRST_RUN)
    assert second[:2] == (0, NEXT_RUN)
    assert counts == [2, 2, 16]
    assert [
        await _count(app_engine, table) for table in ("sellers", "live_sessions", "products")
    ] == counts
    assert PASSWORD not in first[1] + first[2] + second[1] + second[2]
    sellers = SqlSellerRepository(app_engine)
    for slug in ("demo-seller", "sample-shop"):
        seller = await sellers.find_by_slug(slug)
        assert seller is not None
        assert await Argon2PasswordHasher().verify(seller.password_hash, PASSWORD) is True
    sessions = SqlLiveSessionRepository(app_engine)
    for live_id in ("friday-drop", "loadtest"):
        session = await sessions.find_by_live_id(Platform.SIMULATOR, live_id)
        assert session is not None
        assert session.status is LiveSessionStatus.LIVE
    storefront = await ViewStorefront(
        sellers, sessions, SqlProductStockRepository(app_engine, currency="EUR")
    )("demo-seller")
    assert storefront is not None
    assert storefront.session is not None
    assert storefront.session.external_live_id == "friday-drop"


async def _reserve(backend: PostgresBackend, session_id: UUID, code: str, buyer: str) -> None:
    comment_id = await backend.add_comment(f"comment-{code}-{buyer}", author=buyer)
    result = await backend.orders.reserve(
        ReservationRequest(
            comment_id=comment_id,
            session_id=session_id,
            product_code=ProductCode(code),
            variant=Variant(),
            quantity=Quantity(2),
            source=IntentSource.PARSER,
            confidence=Decimal("1.00"),
            evidence=(code,),
            now=FAKE_CLOCK_START,
            reserved_until=FAKE_CLOCK_START + timedelta(seconds=900),
        )
    )
    assert result is not None
    assert result.order is not None


async def test_reset_loadtest_empties_the_load_test_session_only(
    clean_database: None,
    app_engine: AsyncEngine,
    migrator_engine: AsyncEngine,
    integration_settings: IntegrationSettings,
) -> None:
    settings = SeedSettings(
        migrator_url=integration_settings.migrator_url,
        seed_demo_password=SecretStr(PASSWORD),
        _env_file=None,
    )
    report, _ = await seed(settings, reset_loadtest=False)
    loadtest, friday = report.session("loadtest"), report.session("friday-drop")
    backend = PostgresBackend(app_engine, migrator_engine)
    for buyer in ("buyer-1", "buyer-2", "buyer-3"):
        await _reserve(backend, loadtest.id, "Z99", buyer)
    await _reserve(backend, friday.id, "B07", "buyer-1")

    again, reset = await seed(settings, reset_loadtest=True)

    assert reset is not None
    assert (reset.comments, reset.intents, reset.orders) == (3, 3, 3)
    assert again.session("loadtest").id == loadtest.id
    async with app_engine.connect() as connection:
        z99 = (
            await connection.execute(
                select(
                    tables.products.c.stock_total,
                    tables.products.c.reserved,
                    tables.products.c.sold,
                ).where(tables.products.c.session_id == loadtest.id)
            )
        ).one()
        left = (
            await connection.execute(
                select(tables.orders.c.session_id, func.count()).group_by(
                    tables.orders.c.session_id
                )
            )
        ).all()
    assert tuple(z99) == (200, 0, 0)
    # The other session's order, and the comment it came from, are left alone.
    assert [tuple(row) for row in left] == [(friday.id, 1)]
    assert await _count(app_engine, "comments") == 1
    assert await _count(app_engine, "purchase_intents") == 1


async def test_an_ended_load_test_session_is_not_reset(
    clean_database: None, app_engine: AsyncEngine, integration_settings: IntegrationSettings
) -> None:
    settings = SeedSettings(
        migrator_url=integration_settings.migrator_url,
        seed_demo_password=SecretStr(PASSWORD),
        _env_file=None,
    )
    report, _ = await seed(settings, reset_loadtest=False)
    loadtest = report.session("loadtest")
    await SqlLiveSessionRepository(app_engine).set_status(
        loadtest.seller_id, loadtest.id, LiveSessionStatus.ENDED, now=FAKE_CLOCK_START
    )

    # The seed itself goes on: an ended demo session is a state, not an error.
    again, _ = await seed(settings, reset_loadtest=False)
    with pytest.raises(SeedConflict, match="the session loadtest has ended"):
        await seed(settings, reset_loadtest=True)

    assert again.session("loadtest").status is LiveSessionStatus.ENDED
