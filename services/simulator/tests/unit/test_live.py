"""The live audience: Poisson arrivals, batches of what is due, and a bounded catch-up.

Time is a fake clock that the audience's pause moves forward at once, so a
run of an hour of arrivals takes milliseconds and repeats exactly.
"""

import asyncio
import random
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from livecart_core.application.demo_seed import FRIDAY_DROP
from livecart_core.application.dto import MAX_BATCH_SIZE, IncomingComment, Platform
from livecart_simulator.comments import CommentKind, CommentWriter, buyer_ids, catalog_of
from livecart_simulator.live import MAX_LAG_S, MAX_RATE, MIN_RATE, LiveAudience, check_rate
from livecart_simulator.poster import Delivery

POSTED_AT = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)


class FakeTime:
    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    async def pause(self, stop: asyncio.Event, seconds: float) -> None:
        self.now += seconds


@dataclass
class RecordingPoster:
    """Takes every batch at the fake time it arrives; ``busy_s`` makes each post that long."""

    time: FakeTime
    delivery: Delivery = Delivery.ACCEPTED
    busy_s: float = 0.0
    batches: list[tuple[float, list[IncomingComment]]] = field(default_factory=list)

    async def post(self, comments: list[IncomingComment], *, stop: asyncio.Event) -> Delivery:
        self.batches.append((self.time.now, comments))
        self.time.now += self.busy_s
        return self.delivery

    @property
    def comments(self) -> list[IncomingComment]:
        return [comment for _, batch in self.batches for comment in batch]


def _audience(
    poster: RecordingPoster, *, rate: float, seed: int = 3, run_id: str = "run1"
) -> LiveAudience:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    writer = CommentWriter(catalog_of(FRIDAY_DROP), buyers=buyer_ids(12), rng=rng)
    return LiveAudience(
        writer,
        poster,
        external_live_id=FRIDAY_DROP.external_live_id,
        rate=rate,
        rng=rng,
        run_id=run_id,
        monotonic=poster.time.monotonic,
        now=lambda: POSTED_AT,
        pause=poster.time.pause,
    )


async def test_arrivals_are_a_poisson_process_of_the_rate() -> None:
    poster = RecordingPoster(FakeTime())
    start = poster.time.now

    summary = await _audience(poster, rate=0.5).run(asyncio.Event(), count=4_000)

    assert summary.posted == 4_000
    # One comment each time at this rate: the loop wakes up for every arrival.
    times = [at for at, batch in poster.batches for _ in batch]
    gaps = [later - earlier for earlier, later in zip([start, *times], times, strict=False)]
    # Exponential gaps of mean 1 / rate: 2 s, and as spread as they are long.
    assert statistics.fmean(gaps) == pytest.approx(2.0, rel=0.05)
    assert statistics.stdev(gaps) == pytest.approx(2.0, rel=0.08)


async def test_every_comment_is_in_the_common_format_under_a_new_id() -> None:
    poster = RecordingPoster(FakeTime())

    summary = await _audience(poster, rate=1.0, run_id="abcd").run(asyncio.Event(), count=30)

    comments = poster.comments
    assert [comment.external_comment_id for comment in comments] == [
        f"sim-abcd-{number:06d}" for number in range(1, 31)
    ]
    assert {comment.platform for comment in comments} == {Platform.SIMULATOR}
    assert {comment.external_live_id for comment in comments} == {"friday-drop"}
    assert {comment.posted_at for comment in comments} == {POSTED_AT}
    assert {comment.author_platform_user_id for comment in comments} <= set(buyer_ids(12))
    assert sum(summary.kinds.values()) == 30
    assert summary.refused == 0


async def test_arrivals_due_together_go_in_one_batch_of_200_at_most() -> None:
    # Each post takes 10 s of fake time: at 100 a second, the next batch is due in full.
    poster = RecordingPoster(FakeTime(), busy_s=10.0)

    await _audience(poster, rate=MAX_RATE).run(asyncio.Event(), count=1_000)

    sizes = [len(batch) for _, batch in poster.batches]
    assert sum(sizes) == 1_000
    assert max(sizes) == MAX_BATCH_SIZE


async def test_a_long_wait_catches_up_on_five_seconds_of_arrivals_only() -> None:
    # The first post is held for 10 minutes, as by a long 503.
    poster = RecordingPoster(FakeTime(), busy_s=600.0)

    await _audience(poster, rate=2.0).run(asyncio.Event(), count=40)

    # 1200 arrivals were missed; the next batch holds about MAX_LAG_S of them.
    second = len(poster.batches[1][1])
    assert 1 <= second <= 3 * MAX_LAG_S * 2.0


async def test_refused_batches_are_counted_and_not_posted_again() -> None:
    poster = RecordingPoster(FakeTime(), delivery=Delivery.REFUSED)

    summary = await _audience(poster, rate=1.0).run(asyncio.Event(), count=5)

    assert summary.refused == 5
    assert len(poster.comments) == 5


class StoppedWhilePosting(RecordingPoster):
    """A poster stopped while it waited to post again: the batch is abandoned."""

    async def post(self, comments: list[IncomingComment], *, stop: asyncio.Event) -> Delivery:
        stop.set()
        return await super().post(comments, stop=stop)


async def test_a_stop_ends_the_run_and_an_abandoned_batch_is_not_counted() -> None:
    stop = asyncio.Event()
    poster = StoppedWhilePosting(FakeTime(), delivery=Delivery.ABANDONED)

    summary = await _audience(poster, rate=1.0).run(stop)

    assert summary.posted == 0
    assert len(poster.batches) == 1


async def test_a_real_pause_returns_when_stopped() -> None:
    stop = asyncio.Event()
    poster = RecordingPoster(FakeTime())
    rng = random.Random(0)  # noqa: S311 - reproducible test data
    audience = LiveAudience(
        CommentWriter(catalog_of(FRIDAY_DROP), buyers=buyer_ids(1), rng=rng),
        poster,
        external_live_id="friday-drop",
        rate=MIN_RATE,
        rng=rng,
    )

    run = asyncio.create_task(audience.run(stop))
    await asyncio.sleep(0.05)
    stop.set()

    summary = await asyncio.wait_for(run, timeout=2)
    assert summary.posted == 0
    assert summary.kinds == {}
    assert CommentKind.PURCHASE not in summary.kinds


def test_the_rate_and_the_count_are_bounded() -> None:
    assert check_rate(0.2) == 0.2
    for rate in (0.0, MIN_RATE / 2, MAX_RATE * 2):
        with pytest.raises(ValueError, match="comments a second"):
            check_rate(rate)


async def test_a_count_must_be_positive() -> None:
    with pytest.raises(ValueError, match="count must be positive"):
        await _audience(RecordingPoster(FakeTime()), rate=1.0).run(asyncio.Event(), count=0)
