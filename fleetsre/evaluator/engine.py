"""D4 evaluator engine stub."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fleetsre.clock import Clock
from fleetsre.contracts import Contract
from fleetsre.logmodel import LogEntry
from fleetsre.registry import Deployment


@dataclass(frozen=True)
class TripEvent:
    kind: Literal["trip", "recovery"]
    deployment: str
    tripwire: str
    at: datetime
    reason: str
    evidence: tuple[str, ...]


class Evaluator:
    def __init__(
        self,
        contracts: list[Contract],
        deployments: list[Deployment],
        clock: Clock,
    ) -> None:
        raise NotImplementedError

    def ingest(self, entry: LogEntry) -> list[TripEvent]:
        raise NotImplementedError

    def tick(self) -> list[TripEvent]:
        raise NotImplementedError

    def canary_planted(
        self, deployment: str, provider_incident_id: str, at: datetime
    ) -> None:
        raise NotImplementedError

    def enter_change_point(
        self,
        deployment: str,
        component: str,
        *,
        duration_s: int,
        overrides: dict[str, dict[str, Any]],
    ) -> None:
        raise NotImplementedError

    def status(self) -> dict[str, dict[str, Literal["ok", "tripped"]]]:
        raise NotImplementedError
