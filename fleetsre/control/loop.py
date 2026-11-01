"""Control loop: D4 replay ticks driving the incident manager (d6a-pins 9)."""

from datetime import datetime, timedelta
from typing import Any

from fleetsre.harness.replay import replay_tick


class ControlLoop:
    """Steps world, evaluator and manager on a shared tick grid."""

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
        self._world = world
        self._evaluator = evaluator
        self._scheduler = scheduler
        self._manager = manager
        self._sink = sink
        self._t0 = t0
        self._tick_seconds = tick_seconds
        self._index = 0
        self._consumed = 0

    def step(self) -> None:
        """Run one tick: replay step, route events, drive the manager."""
        tick_at = self._t0 + timedelta(
            seconds=self._index * self._tick_seconds
        )
        self._index += 1
        events, self._consumed = replay_tick(
            self._world.clock,
            self._scheduler,
            self._world,
            self._sink,
            self._evaluator,
            self._consumed,
            tick_at,
        )
        for event in events:
            self._manager.on_trip(event)
        self._manager.tick()

    def run_until(self, t: datetime) -> None:
        """Step until the tick grid has passed `t`."""
        while (
            self._t0
            + timedelta(seconds=self._index * self._tick_seconds)
            <= t
        ):
            self.step()
