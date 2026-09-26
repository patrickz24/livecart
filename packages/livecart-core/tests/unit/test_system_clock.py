from datetime import UTC, datetime, timedelta

from livecart_core.application.ports import Clock
from livecart_core.infrastructure.clock import SystemClock


def test_the_system_clock_tells_the_current_time_in_utc() -> None:
    clock: Clock = SystemClock()

    before = datetime.now(UTC)
    now = clock.now()

    assert now.tzinfo is UTC
    assert before <= now <= datetime.now(UTC) + timedelta(seconds=1)
