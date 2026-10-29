"""D1 scenario stub: chaos harness scenario library (spec.md §5.1)."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict


class Fault(BaseModel):
    model_config = ConfigDict(extra="allow")

    at: int
    type: str
    environment: str
    params: dict[str, Any] = {}
    clearAt: int | None = None


class ExpectedTrip(BaseModel):
    deployment: str
    tripwire: str
    withinSeconds: int


class ExpectedRecovery(BaseModel):
    deployment: str
    tripwire: str
    withinSeconds: int


class MustNotTrip(BaseModel):
    deployment: str
    tripwire: str


class Expected(BaseModel):
    trips: list[ExpectedTrip] = []
    recoveries: list[ExpectedRecovery] = []
    mustNotTrip: list[MustNotTrip] = []


class Scenario(BaseModel):
    id: str
    service: str
    seed: int
    durationSeconds: int
    deployments: list[str]
    faults: list[Fault] = []
    expected: Expected
    holdout: bool = False


def load_scenario(path: str | Path) -> Scenario:
    """Load and validate a scenario YAML file."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Scenario.model_validate(data)
