"""A probe of an engine's connection pool, for the tests that must prove real contention."""

from typing import Any, Self

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine


class PoolProbe:
    """Counts the connections checked out of an engine's pool: in all, and at most at once.

    A concurrency test that never saturated the pool proved nothing, since
    its units may have run one after the other: the tests assert ``peak``.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.checked_out = 0
        self.peak = 0
        self.checkouts = 0

    def __enter__(self) -> Self:
        event.listen(self._engine.sync_engine, "checkout", self._on_checkout)
        event.listen(self._engine.sync_engine, "checkin", self._on_checkin)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self._engine.sync_engine, "checkout", self._on_checkout)
        event.remove(self._engine.sync_engine, "checkin", self._on_checkin)

    def _on_checkout(self, *_: Any) -> None:
        self.checked_out += 1
        self.checkouts += 1
        self.peak = max(self.peak, self.checked_out)

    def _on_checkin(self, *_: Any) -> None:
        self.checked_out -= 1
