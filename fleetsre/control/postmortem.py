"""Code-built postmortem (d6a-pins 7, 17). No model writes it."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class TimelineEvent(BaseModel):
    at: datetime
    kind: str
    detail: dict[str, Any] = {}


class Postmortem(BaseModel):
    incident_id: str
    deployment: str
    service: str
    opened_at: datetime
    closed_at: datetime
    outcome: Literal["resolved", "escalated"]
    category: str | None
    root_cause: str | None
    action: dict[str, Any] | None
    timeline: list[TimelineEvent]
    transcript_ids: list[str]


def build_postmortem(
    record: Any, *, closed_at: datetime, outcome: Literal["resolved", "escalated"]
) -> Postmortem:
    """Build the postmortem from the incident record (pin 17)."""
    diagnosis = record.diagnosis or {}
    if diagnosis:
        category = diagnosis.get("category")
        root_cause = diagnosis.get("summary")
        act = diagnosis.get("action") or {}
        action: dict[str, Any] | None = {
            key: act.get(key)
            for key in ("type", "method", "resource", "updateMask", "patch")
        }
    else:
        category = None
        root_cause = None
        action = None
    incident = record.incident
    return Postmortem(
        incident_id=record.id,
        deployment=incident.deployment,
        service=incident.service,
        opened_at=incident.opened_at,
        closed_at=closed_at,
        outcome=outcome,
        category=category,
        root_cause=root_cause,
        action=action,
        timeline=list(record.timeline),
        transcript_ids=list(record.transcript_ids),
    )
