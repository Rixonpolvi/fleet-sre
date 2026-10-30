"""Clock port: SimClock for tests, WallClock for production (D3 stub)."""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SimClock(Clock):
    def __init__(self, start: datetime) -> None:
        raise NotImplementedError

    def now(self) -> datetime:
        raise NotImplementedError

    def advance(self, seconds: float) -> None:
        raise NotImplementedError

    def set(self, t: datetime) -> None:
        raise NotImplementedError


class WallClock(Clock):
    def now(self) -> datetime:
        raise NotImplementedError
