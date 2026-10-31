"""SRE schemas: hypotheses, evidence, diagnosis, incident (spec.md §6)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class Hypothesis(BaseModel):
    id: str
    statement: str
    wouldConfirm: str
    alternative: bool


class Evidence(BaseModel):
    id: str
    hypothesis: str
    stance: Literal["supports", "refutes"]
    kind: Literal["log", "toolResult"]
    insertId: str | None = None
    callId: str | None = None
    field: str | None = None
    value: Any = None
    why: str = ""
    recordedAtTurn: int = 0
    deployment: str | None = None


class OpenQuestion(BaseModel):
    hypothesis: str = ""
    why: str = ""


class Action(BaseModel):
    type: Literal["propose", "none", "escalate"]
    method: str | None = None
    resource: str | None = None
    updateMask: list[str] | None = None
    patch: dict[str, Any] | None = None
    escalationSummary: str | None = None


class ExpectedVerification(BaseModel):
    tripwire: str = ""
    condition: str = ""
    seconds: int = 0


class Diagnosis(BaseModel):
    incidentId: str
    deployment: str
    component: str
    category: Literal[
        "connector_stalled",
        "job_stalled",
        "credential_failure",
        "schema_drift",
        "job_hollow_success",
        "job_failing",
        "unknown",
    ]
    hypothesis: str
    summary: str = ""
    evidence: list[str] = []
    openQuestions: list[OpenQuestion] = []
    action: Action
    expectedVerification: ExpectedVerification | None = None
    rollback: str | None = None
    confidence: Literal["low", "medium", "high"] = "medium"


class Incident(BaseModel):
    id: str
    deployment: str
    tripwire: str
    opened_at: datetime
    trip_event: Any
    service: str = ""
    component: str = ""
    resources: dict[str, str] = {}
    siblings: list[str] = []

    @classmethod
    def from_trip(
        cls, event: Any, registry: Any, now: datetime
    ) -> "Incident":
        raise NotImplementedError


def render_incident(incident: Incident, registry: Any) -> str:
    """Build the first user message for an incident."""
    raise NotImplementedError


READ_TOOL_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {}
