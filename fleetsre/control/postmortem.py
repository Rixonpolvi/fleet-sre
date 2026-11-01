"""Code-built postmortem (d6a-pins 7, 17). Stub: builder raises."""

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
    record: Any, *, closed_at: datetime, outcome: str
) -> Postmortem:
    raise NotImplementedError
