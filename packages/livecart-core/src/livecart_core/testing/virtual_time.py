"""An event loop on the test's clock: it stands still while code runs, and jumps to the next timer.

For a test whose outcome hangs on a timer against what ``loop.time()`` reads,
a budget for instance. On the system's clock, asyncio fires a timer up to one
tick of that clock before ``loop.time()`` reaches it (15.6 ms on Windows,
where ``time.monotonic`` is ``GetTickCount64``), and a loaded machine stalls a
task between two readings: the same test then sees the deadline passed, or
not. On ``VirtualTimeLoop`` no time passes while a task runs; once every task
waits, the clock jumps to the next timer, exactly, and fires it. A 60 s sleep
takes no time, and a deadline fires as ``loop.time()`` reads it.

Only for code that waits on timers and on its own tasks: while a timer is
pending, the loop jumps to it rather than wait for real I/O or for a thread.
"""

import asyncio
import functools
import selectors
from collections.abc import Callable, Coroutine


class _JumpingSelector(selectors.DefaultSelector):
    """Never waits: when nothing is ready, the clock jumps by the wait the loop asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        # The loop asks for the time left until its next timer, or for no
        # limit when it has none: that wait, for a thread or for real I/O, is
        # left to the system.
        ready = super().select(None if timeout is None else 0)
        if not ready and timeout is not None:
            self.now += timeout
        return ready


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """See the module docstring: ``loop.time()`` reads the virtual clock, from 0."""

    def __init__(self) -> None:
        self._jumping = _JumpingSelector()
        super().__init__(self._jumping)
        # asyncio fires a timer once ``loop.time()`` is within this of it, the
        # resolution of the system's clock by default: here the clock is
        # exact, and a timer fires once it reads the timer's time.
        self._clock_resolution = 1e-9

    def time(self) -> float:
        return self._jumping.now


def on_virtual_time[**P](test: Callable[P, Coroutine[object, object, None]]) -> Callable[P, None]:
    """Runs an async test on a ``VirtualTimeLoop`` of its own, instead of pytest-asyncio's loop."""

    @functools.wraps(test)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(test(*args, **kwargs), loop_factory=VirtualTimeLoop)

    return run
