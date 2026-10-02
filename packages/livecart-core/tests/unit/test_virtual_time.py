"""``VirtualTimeLoop``: a clock that stands still while code runs, and jumps to the next timer."""

import asyncio

import pytest

from livecart_core.testing.virtual_time import on_virtual_time


@on_virtual_time
async def test_a_deadline_fires_as_the_clock_reads_it_and_a_long_sleep_takes_no_time() -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 0.1

    with pytest.raises(TimeoutError):
        async with asyncio.timeout_at(deadline):
            await asyncio.sleep(3600)

    assert loop.time() == deadline
    await asyncio.sleep(3600)
    assert loop.time() == deadline + 3600


@on_virtual_time
async def test_a_timer_within_a_tick_of_the_system_clock_waits_for_its_time() -> None:
    loop = asyncio.get_running_loop()
    fired_at: list[float] = []
    # 1 ms: within a tick of GetTickCount64 (15.6 ms), in which asyncio would
    # fire it at the loop's next turn, at 0, on the system's clock.
    loop.call_later(0.001, lambda: fired_at.append(loop.time()))

    for _ in range(10):
        await asyncio.sleep(0)
    assert fired_at == []

    await asyncio.sleep(0.002)
    assert fired_at == [pytest.approx(0.001)]


@on_virtual_time
async def test_the_clock_stands_still_while_the_tasks_run() -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()

    await asyncio.gather(*(asyncio.sleep(0) for _ in range(100)))
    sum(range(1_000_000))

    assert loop.time() == started
