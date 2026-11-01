"""Incident lifecycle (d6a-pins 8, 10-17). Stub: manager raises."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class IncidentState(StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    NEEDS_INPUT = "NEEDS_INPUT"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPLYING = "APPLYING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"


class Pending(BaseModel):
    kind: Literal["start", "resume", "reopen"]
    message: str | None = None
    extra_turns: int = 0
    trigger: str


class IncidentRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    incident: Any
    state: IncidentState
    tripwires: list[str] = []
    reason: str | None = None
    paused_at: datetime | None = None
    paused_snapshot: dict[str, str] | None = None
    pending: Pending | None = None
    diagnosis: dict[str, Any] | None = None
    proposals: list[Any] = []
    failed_attempts: int = 0
    timeline: list[Any] = []
    transcript_ids: list[str] = []
    postmortem: Any | None = None


class IncidentManager:
    def __init__(
        self,
        evaluator: Any,
        investigator: Any,
        approvals: Any,
        executor: Any,
        verifier: Any,
        state: Any,
        clock: Any,
        *,
        registry: Any,
        reader: Any,
    ) -> None:
        raise NotImplementedError

    def on_trip(self, event: Any) -> Any | None:
        raise NotImplementedError

    def tick(self) -> None:
        raise NotImplementedError

    def approve(
        self, incident_id: str, proposal_hash: str, *, by: str
    ) -> Any:
        raise NotImplementedError

    def reject(
        self, incident_id: str, proposal_hash: str, *, by: str, notes: str
    ) -> Any:
        raise NotImplementedError

    def reply(
        self, incident_id: str, message: str, *, by: str, extra_turns: int = 0
    ) -> None:
        raise NotImplementedError

    def close(self, incident_id: str, *, by: str, note: str) -> None:
        raise NotImplementedError

    def get(self, incident_id: str) -> IncidentRecord:
        raise NotImplementedError

    def incidents(self) -> list[IncidentRecord]:
        raise NotImplementedError
