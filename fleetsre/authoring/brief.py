"""Service brief for contract authoring (d6b-pins 1). Stub: loading raises."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class BriefComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    description: str
    logs: dict[str, Any]


class BriefParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    description: str


class BriefEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    container: str
    description: str


class BriefCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    component: str
    detects: str


class ServiceBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    version: str
    summary: str
    components: list[BriefComponent]
    parameters: dict[str, BriefParameter]
    events: list[BriefEvent]
    checks: list[BriefCheck]
    notes: list[str]
    sample_files: list[str]


def load_brief(path: str | Path) -> ServiceBrief:
    """Read a service brief YAML file (d6b-pins 1)."""
    raise NotImplementedError
