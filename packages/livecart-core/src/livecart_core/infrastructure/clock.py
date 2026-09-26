"""The ``Clock`` port in production: the system's time, in UTC."""

from datetime import UTC, datetime


class SystemClock:
    """The current time, always timezone-aware UTC, as every deadline and lease expects."""

    def now(self) -> datetime:
        return datetime.now(UTC)
