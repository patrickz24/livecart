import asyncio
import signal
from pathlib import Path

import pytest
from pydantic import SecretStr

from livecart_core.settings import Settings
from livecart_worker.main import install_stop_handlers, serve


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
async def test_stop_signals_set_the_stop_event(signum: signal.Signals) -> None:
    stop = asyncio.Event()
    restore = install_stop_handlers(asyncio.get_running_loop(), stop)
    try:
        signal.raise_signal(signum)
        await asyncio.wait_for(stop.wait(), timeout=2)
    finally:
        restore()

    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL


async def test_serve_returns_once_stopped_and_releases_redis(tmp_path: Path) -> None:
    heartbeat = tmp_path / "worker.heartbeat"
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@db.invalid/livecart"),
        # A closed port: the stop event is already set, so no PING is attempted.
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        worker_heartbeat_file=heartbeat,
        _env_file=None,
    )
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(serve(settings, stop), timeout=2)

    assert not heartbeat.exists()
