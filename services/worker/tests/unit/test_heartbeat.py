import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from livecart_core.healthcheck import check_heartbeat
from livecart_worker.heartbeat import run_heartbeat

FAST = 0.01


def start(
    ping: Callable[[], Awaitable[None]], path: Path, stop: asyncio.Event, **kwargs: float
) -> asyncio.Task[None]:
    options = {"interval_s": FAST, "ping_timeout_s": 0.5, **kwargs}
    return asyncio.create_task(run_heartbeat(ping=ping, path=path, stop=stop, **options))


async def test_a_successful_ping_touches_the_file(tmp_path: Path) -> None:
    path, stop = tmp_path / "worker.heartbeat", asyncio.Event()
    seen_healthy: list[bool] = []

    async def ping() -> None:
        # Called before each beat: from the second call on, the first beat is visible.
        if path.exists() or seen_healthy:
            seen_healthy.append(check_heartbeat(path, max_age_s=30))
            stop.set()

    await asyncio.wait_for(start(ping, path, stop), timeout=2)

    assert seen_healthy == [True]
    assert not path.exists()  # removed on shutdown


async def test_a_failed_ping_never_refreshes_the_file(tmp_path: Path) -> None:
    path, stop = tmp_path / "worker.heartbeat", asyncio.Event()
    path.touch()
    stale = time.time() - 120
    os.utime(path, (stale, stale))
    calls, second_ping_failed = 0, asyncio.Event()

    async def ping() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            second_ping_failed.set()
        if calls == 3:
            stop.set()
        raise ConnectionError("redis down")

    task = start(ping, path, stop)
    await asyncio.wait_for(second_ping_failed.wait(), timeout=2)
    assert path.stat().st_mtime == pytest.approx(stale)
    assert not check_heartbeat(path, max_age_s=30)
    await asyncio.wait_for(task, timeout=2)


async def test_a_hanging_ping_is_bounded_and_the_loop_keeps_beating(tmp_path: Path) -> None:
    path, stop = tmp_path / "worker.heartbeat", asyncio.Event()
    calls = 0

    async def ping() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            stop.set()
            return
        await asyncio.Event().wait()

    await asyncio.wait_for(start(ping, path, stop, ping_timeout_s=0.05), timeout=2)

    assert calls == 3


async def test_stop_interrupts_the_wait_between_beats(tmp_path: Path) -> None:
    path, stop = tmp_path / "worker.heartbeat", asyncio.Event()
    beat = asyncio.Event()

    async def ping() -> None:
        beat.set()

    task = start(ping, path, stop, interval_s=60)
    await asyncio.wait_for(beat.wait(), timeout=2)
    started = time.perf_counter()
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert time.perf_counter() - started < 1
    assert not path.exists()


async def test_cancellation_also_removes_the_file(tmp_path: Path) -> None:
    path, stop = tmp_path / "worker.heartbeat", asyncio.Event()

    calls, second_ping = 0, asyncio.Event()

    async def ping() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # the first beat has touched the file by now
            second_ping.set()

    task = start(ping, path, stop)
    await asyncio.wait_for(second_ping.wait(), timeout=2)
    assert path.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not path.exists()
