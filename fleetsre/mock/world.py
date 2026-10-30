"""Mock world (D3 stub)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from fleetsre.clock import Clock
from fleetsre.mock.fleet import FleetConfig
from fleetsre.ports import LogSink


class FaultSpec(BaseModel):
    type: str
    environment: str
    target: str | None = None
    params: dict[str, Any] = {}


class World:
    def __init__(
        self, fleet: FleetConfig, seed: int, clock: Clock, sink: LogSink
    ) -> None:
        raise NotImplementedError

    def step_until(self, t: datetime) -> None:
        raise NotImplementedError

    def inject(self, fault: FaultSpec) -> str:
        raise NotImplementedError

    def clear(self, fault_id: str) -> None:
        raise NotImplementedError

    def plant_canary(self, environment: str) -> str:
        raise NotImplementedError

    def snapshot(self) -> dict[str, Any]:
        raise NotImplementedError
