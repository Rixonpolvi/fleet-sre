"""Clock port: SimClock for tests and replay, WallClock for production."""

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SimClock(Clock):
    """Deterministic clock; the world sets it as the schedule advances."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, t: datetime) -> None:
        self._now = t


class WallClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
