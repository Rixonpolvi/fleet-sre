"""D1 contracts stub: service contract loader (spec.md §5)."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class ContractError(Exception):
    """Loader failure with a code from deliverables.md §D1."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class Parameter(BaseModel):
    type: str
    required: bool
    default: Any = None
    description: str | None = None


class LogsConfig(BaseModel):
    container_name: str
    match: list[str]
    deploymentBy: str


class Component(BaseModel):
    id: str
    kind: str
    logs: LogsConfig
    description: str | None = None


class Tripwire(BaseModel):
    id: str
    component: str
    type: str
    spec: dict[str, Any]
    description: str | None = None


class ChangePoint(BaseModel):
    durationSeconds: int
    overrides: dict[str, dict[str, Any]]


class Provenance(BaseModel):
    authoredBy: str
    scorecard: str | None = None
    approvedBy: str | None = None


class Contract(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: str
    version: str
    description: str | None = None
    parameters: dict[str, Parameter]
    components: list[Component]
    tripwires: list[Tripwire]
    changePoint: ChangePoint
    provenance: Provenance


def load_contract(path: str | Path) -> Contract:
    """Validate against contracts/schema.json plus referential rules. Session B implements."""
    raise NotImplementedError
