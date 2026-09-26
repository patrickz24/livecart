"""The worker loops against the compose Redis: a real PING refreshes the heartbeat."""

import asyncio
from pathlib import Path

import pytest

from livecart_core.healthcheck import check_heartbeat
from livecart_core.settings import Settings
from livecart_core.testing.database import IntegrationSettings
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY
from livecart_worker.main import serve

pytestmark = pytest.mark.integration


async def test_a_real_ping_keeps_the_heartbeat_fresh(
    integration_settings: IntegrationSettings, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "worker.heartbeat"
    settings = Settings(
        database_url=integration_settings.database_url,
        redis_url=integration_settings.redis_url,
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        worker_heartbeat_file=heartbeat,
        _env_file=None,
    )
    stop = asyncio.Event()
    worker = asyncio.create_task(serve(settings, stop, heartbeat_interval_s=0.05))

    async def beaten() -> None:
        while not heartbeat.exists():  # noqa: ASYNC110 - polling a file, not an event
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(beaten(), timeout=5)
        assert check_heartbeat(heartbeat, max_age_s=30)
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=5)

    assert not heartbeat.exists()
