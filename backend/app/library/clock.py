"""Where the library gets the current time.

Every expiry decision (sessions, the 7-day restore window, purge due times)
reads the time from a Clock handed in by the caller, so tests can move time
forward instead of waiting seven days.
"""
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Current UTC time, optionally shifted forward for local verification."""

    def __init__(self, offset: timedelta = timedelta(0)):
        self.offset = offset

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset
