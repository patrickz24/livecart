import asyncio
import signal
from pathlib import Path

import pytest
from pydantic import SecretStr

from livecart_core.application.dto import FallbackReceipt
from livecart_core.application.llm_guard import LlmGuard
from livecart_core.application.ports import NullIntentFallback
from livecart_core.domain.outcomes import LlmOutcome
from livecart_core.settings import LLM_MAX_RETRIES, AppEnv, Settings
from livecart_core.testing.fakes import FakeClock, InMemoryLlmBudget
from livecart_core.testing.keys import TEST_JWT_SECRET, TEST_ORDER_TOKEN_KEY
from livecart_worker.main import install_stop_handlers, llm_fallback, serve
from livecart_worker.metrics import WorkerMetrics


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
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        worker_heartbeat_file=heartbeat,
        _env_file=None,
    )
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(serve(settings, stop), timeout=2)

    assert not heartbeat.exists()


async def test_a_production_worker_refuses_the_simulated_notifier(tmp_path: Path) -> None:
    settings = Settings(
        app_env=AppEnv.PRODUCTION,
        database_url=SecretStr("postgresql+asyncpg://u:p@db.invalid/livecart"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        worker_heartbeat_file=tmp_path / "worker.heartbeat",
        _env_file=None,
    )

    with pytest.raises(ValueError, match="refused when APP_ENV=production"):
        await serve(settings, asyncio.Event())


def _settings(*, key: str | None, enabled: bool = True) -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@db.invalid/livecart"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        anthropic_api_key=None if key is None else SecretStr(key),
        llm_enabled=enabled,
        _env_file=None,
    )


@pytest.mark.parametrize(("key", "enabled"), [(None, True), ("sk-ant-test", False)])
def test_without_a_key_or_once_disabled_the_worker_runs_the_parser_alone(
    key: str | None, enabled: bool
) -> None:
    fallback, client = llm_fallback(
        _settings(key=key, enabled=enabled), budget=InMemoryLlmBudget(), clock=FakeClock()
    )

    assert isinstance(fallback, NullIntentFallback)
    assert client is None


async def test_with_a_key_the_worker_asks_claude_through_its_guard_and_one_client() -> None:
    fallback, client = llm_fallback(
        _settings(key="sk-ant-test"), budget=InMemoryLlmBudget(), clock=FakeClock()
    )

    assert isinstance(fallback, LlmGuard)
    assert fallback.cost_of(FallbackReceipt(LlmOutcome.TIMEOUT)) == 0
    assert client is not None
    assert client.max_retries == LLM_MAX_RETRIES
    await client.close()


async def test_serve_scrapes_the_llm_counters_once_the_fallback_is_on(tmp_path: Path) -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@db.invalid/livecart"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        jwt_secret=TEST_JWT_SECRET,
        order_token_key=TEST_ORDER_TOKEN_KEY,
        worker_heartbeat_file=tmp_path / "worker.heartbeat",
        anthropic_api_key=SecretStr("sk-ant-test"),
        _env_file=None,
    )
    stop = asyncio.Event()
    stop.set()
    metrics = WorkerMetrics()

    await asyncio.wait_for(serve(settings, stop, metrics=metrics), timeout=2)

    busy = {"outcome": "llm_busy"}
    assert metrics.registry.get_sample_value("livecart_llm_outcomes_total", busy) == 0
    assert metrics.registry.get_sample_value("livecart_llm_spend_usd_total") == 0
