from datetime import UTC, datetime, timedelta

import pytest

from livecart_core.testing.fakes import FAKE_CLOCK_START, FakeClock


def test_the_clock_starts_at_a_fixed_aware_instant() -> None:
    clock = FakeClock()

    assert clock.now() == FAKE_CLOCK_START
    assert clock.now().utcoffset() == timedelta(0)


def test_the_clock_only_moves_when_told() -> None:
    clock = FakeClock()

    assert clock.now() == clock.now()
    assert clock.advance(1.5) == FAKE_CLOCK_START + timedelta(seconds=1.5)
    assert clock.advance(timedelta(minutes=2)) == FAKE_CLOCK_START + timedelta(seconds=121.5)
    assert clock.now() == FAKE_CLOCK_START + timedelta(seconds=121.5)


def test_set_goes_anywhere_advance_only_forward() -> None:
    clock = FakeClock()
    earlier = datetime(2026, 1, 1, tzinfo=UTC)

    clock.set(earlier)

    assert clock.now() == earlier
    with pytest.raises(ValueError, match="only moves forward"):
        clock.advance(-1)


def test_the_clock_refuses_naive_datetimes() -> None:
    naive = FAKE_CLOCK_START.replace(tzinfo=None)

    with pytest.raises(ValueError, match="naive"):
        FakeClock(naive)
    with pytest.raises(ValueError, match="naive"):
        FakeClock().set(naive)
