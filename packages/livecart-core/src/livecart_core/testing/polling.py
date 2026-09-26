"""Waiting in a test for state that another task changes and that signals no event."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable


async def until(
    condition: Callable[[], bool | Awaitable[bool]],
    *,
    within_s: float = 2.0,
    every_s: float = 0.005,
) -> None:
    """Poll ``condition``, plain or awaitable, until it holds; ``TimeoutError`` after ``within_s``.

    For a background loop's effects (a delivery acknowledged, a row written,
    a queue drained), which the code under test rightly does not announce.
    """
    async with asyncio.timeout(within_s):
        while True:
            held = condition()
            if await held if inspect.isawaitable(held) else held:
                return
            await asyncio.sleep(every_s)
