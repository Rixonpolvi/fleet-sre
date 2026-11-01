"""Approval records (d6a-pins 2). Stub: behavior raises."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class Approval(BaseModel):
    id: str
    incident_id: str
    proposal_hash: str
    approver: str
    at: datetime


class Rejection(BaseModel):
    id: str
    incident_id: str
    proposal_hash: str
    approver: str
    notes: str
    at: datetime


class ApprovalStore:
    """Records approvals/rejections only; moves no incident (pin 2)."""

    def __init__(self, clock: Any) -> None:
        raise NotImplementedError

    def approve(
        self, incident_id: str, proposal_hash: str, approver: str
    ) -> Approval:
        raise NotImplementedError

    def reject(
        self,
        incident_id: str,
        proposal_hash: str,
        approver: str,
        notes: str,
    ) -> Rejection:
        raise NotImplementedError

    def approval_for(self, proposal_hash: str) -> Approval | None:
        raise NotImplementedError

    def records(self) -> list[Any]:
        raise NotImplementedError
