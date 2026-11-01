"""Control loop on a SimClock (d6a-pins 9). Stub: behavior raises."""

from datetime import datetime
from typing import Any


class ControlLoop:
    def __init__(
        self,
        world: Any,
        evaluator: Any,
        scheduler: Any,
        manager: Any,
        sink: Any,
        *,
        t0: datetime,
        tick_seconds: int = 15,
    ) -> None:
        raise NotImplementedError

    def step(self) -> None:
        raise NotImplementedError

    def run_until(self, t: datetime) -> None:
        raise NotImplementedError
