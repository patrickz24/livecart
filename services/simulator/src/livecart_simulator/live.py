"""The live audience: comments arriving as a Poisson process, posted in batches.

A live audience comments independently, one viewer at a time, so the
comments form a Poisson process: the gaps between two arrivals are
exponential, of mean ``1 / rate`` (``random.expovariate``). Quiet stretches
and small bursts alternate, as in a real live, rather than a metronome.

The loop sleeps until the next arrival, then posts every comment due by then
as one batch (200 at most, the webhook's bound): at a gentle rate each batch
holds one comment, at a high rate the batches grow, as a platform would
group its calls. A batch the API makes wait (503, 429) holds the loop, and
the arrivals it missed meanwhile are dropped past ``MAX_LAG_S`` rather than
all posted at once when the API comes back: the simulator shows a live, not
a replay.
"""

import asyncio
import contextlib
import random
import secrets
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

import structlog

from livecart_core.application.dto import MAX_BATCH_SIZE, IncomingComment, Platform
from livecart_simulator.comments import CommentKind, CommentWriter, SimulatedComment
from livecart_simulator.poster import Delivery

logger = structlog.get_logger(__name__)

# The bounds of the rate, in comments per second: from one every 100 s to the
# load test's order of magnitude, above which the webhook's rate limit bites.
MIN_RATE: Final = 0.01
MAX_RATE: Final = 100.0
# The longest the loop catches up on arrivals it missed while a batch waited.
MAX_LAG_S: Final = 5.0


class BatchPoster(Protocol):
    """Where the batches go: ``WebhookPoster`` in the simulator."""

    async def post(self, comments: list[IncomingComment], *, stop: asyncio.Event) -> Delivery: ...


@dataclass(slots=True)
class RunSummary:
    """What a run posted: the comments of each kind, and those the API refused."""

    kinds: Counter[CommentKind] = field(default_factory=Counter)
    refused: int = 0

    @property
    def posted(self) -> int:
        return sum(self.kinds.values())


def check_rate(rate: float) -> float:
    if not MIN_RATE <= rate <= MAX_RATE:
        raise ValueError(f"the rate is from {MIN_RATE} to {MAX_RATE} comments a second, not {rate}")
    return rate


class LiveAudience:
    """Comments on one live session at ``rate`` a second; see the module docstring.

    Comment ids are ``sim-<run>-<number>``: ``run`` is random per process, so
    that a restarted simulator never reuses the ids of the previous one, which
    the inbox would take for duplicates.
    """

    def __init__(
        self,
        writer: CommentWriter,
        poster: BatchPoster,
        *,
        external_live_id: str,
        rate: float,
        rng: random.Random,
        run_id: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        pause: Callable[[asyncio.Event, float], Awaitable[None]] | None = None,
    ) -> None:
        self._writer = writer
        self._poster = poster
        self._live_id = external_live_id
        self._rate = check_rate(rate)
        self._rng = rng
        self._run_id = run_id if run_id is not None else secrets.token_hex(4)
        self._monotonic = monotonic
        self._now = now
        # Waits for the next arrival, or less once stopped; the tests pass one
        # that moves their clock instead.
        self._pause = pause if pause is not None else _pause
        self._sequence = 0

    async def run(self, stop: asyncio.Event, *, count: int | None = None) -> RunSummary:
        """Post until ``stop`` is set, or until ``count`` comments are posted."""
        if count is not None and count < 1:
            raise ValueError(f"count must be positive, not {count}")
        summary = RunSummary()
        logger.info("simulator_started", live_id=self._live_id, rate=self._rate, count=count)
        next_at = self._monotonic() + self._gap()
        while not stop.is_set() and (count is None or summary.posted < count):
            wait_s = next_at - self._monotonic()
            if wait_s > 0:
                await self._pause(stop, wait_s)
                continue
            now = self._monotonic()
            next_at = max(next_at, now - MAX_LAG_S)
            room = MAX_BATCH_SIZE if count is None else min(MAX_BATCH_SIZE, count - summary.posted)
            due: list[SimulatedComment] = []
            while next_at <= now and len(due) < room:
                due.append(self._writer.write())
                next_at += self._gap()
            await self._post(due, stop, summary)
        logger.info(
            "simulator_stopped",
            posted=summary.posted,
            refused=summary.refused,
            **{kind.value: summary.kinds[kind] for kind in CommentKind},
        )
        return summary

    def incoming(self, comment: SimulatedComment) -> IncomingComment:
        """The comment in the webhook's common format, under the next id of this run."""
        self._sequence += 1
        return IncomingComment(
            platform=Platform.SIMULATOR,
            external_comment_id=f"sim-{self._run_id}-{self._sequence:06d}",
            external_live_id=self._live_id,
            author_platform_user_id=comment.author,
            text=comment.text,
            posted_at=self._now(),
        )

    async def _post(
        self, due: list[SimulatedComment], stop: asyncio.Event, summary: RunSummary
    ) -> None:
        delivery = await self._poster.post([self.incoming(c) for c in due], stop=stop)
        if delivery is Delivery.ABANDONED:
            return
        summary.kinds.update(comment.kind for comment in due)
        if delivery is Delivery.REFUSED:
            summary.refused += len(due)
            return
        logger.info(
            "comments_posted",
            size=len(due),
            **{kind.value: sum(c.kind is kind for c in due) for kind in CommentKind},
        )

    def _gap(self) -> float:
        return self._rng.expovariate(self._rate)


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await stop.wait()
