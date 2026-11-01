"""Approval records (d6a-pins 2). Records only; moves no incident."""

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
        self._clock = clock
        self._records: list[Approval | Rejection] = []
        self._approvals = 0
        self._rejections = 0

    def approve(
        self, incident_id: str, proposal_hash: str, approver: str
    ) -> Approval:
        self._approvals += 1
        record = Approval(
            id=f"A{self._approvals}",
            incident_id=incident_id,
            proposal_hash=proposal_hash,
            approver=approver,
            at=self._clock.now(),
        )
        self._records.append(record)
        return record

    def reject(
        self,
        incident_id: str,
        proposal_hash: str,
        approver: str,
        notes: str,
    ) -> Rejection:
        if not notes or not notes.strip():
            raise ValueError("rejection notes must not be empty")
        self._rejections += 1
        record = Rejection(
            id=f"R{self._rejections}",
            incident_id=incident_id,
            proposal_hash=proposal_hash,
            approver=approver,
            notes=notes,
            at=self._clock.now(),
        )
        self._records.append(record)
        return record

    def approval_for(self, proposal_hash: str) -> Approval | None:
        found: Approval | None = None
        for record in self._records:
            if (
                isinstance(record, Approval)
                and record.proposal_hash == proposal_hash
            ):
                found = record
        return found

    def records(self) -> list[Any]:
        return list(self._records)
