"""Incident lifecycle: open, investigate, approve, apply, verify (pins 8-17)."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from fleetsre.control.executor import (
    ApplyError,
    Conflict,
    InvalidPatch,
    NotApproved,
)
from fleetsre.control.postmortem import (
    TimelineEvent,
    build_postmortem,
)
from fleetsre.control.proposals import (
    Proposal,
    proposal_hash,
    resource_version,
)
from fleetsre.sre.schemas import Diagnosis, Incident


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


_CLOSED = frozenset({IncidentState.RESOLVED, IncidentState.ESCALATED})


def _iso_z(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


class IncidentManager:
    """Owns every incident record; only pending segments call the model."""

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
        if investigator.tools.verification is False:
            raise ValueError(
                "incident manager refuses an investigator "
                "with verification=False"
            )
        if reader.identity != "obs-reader":
            raise ValueError(
                "incident manager reader needs the obs-reader identity, "
                f"got {reader.identity!r}"
            )
        self._evaluator = evaluator
        self._investigator = investigator
        self._approvals = approvals
        self._executor = executor
        self._verifier = verifier
        self._state = state
        self._clock = clock
        self._registry = registry
        self._reader = reader
        self._records: dict[str, IncidentRecord] = {}
        self._verifying: dict[str, dict[str, Any]] = {}

    def on_trip(self, event: Any) -> Any | None:
        """Route one evaluator event: recover, attach, or open (pin 10)."""
        self._state.record_trip(event)
        now = self._clock.now()
        if event.kind == "recovery":
            open_record = self._open_for(event.deployment)
            if open_record is not None:
                open_record.timeline.append(TimelineEvent(
                    at=now, kind="recovery",
                    detail={"tripwire": event.tripwire},
                ))
            return None
        open_record = self._open_for(event.deployment)
        if open_record is not None:
            if event.tripwire not in open_record.tripwires:
                open_record.tripwires.append(event.tripwire)
            open_record.timeline.append(TimelineEvent(
                at=now, kind="attached",
                detail={"tripwire": event.tripwire},
            ))
            return open_record.incident
        incident = Incident.from_trip(event, self._registry, now)
        record = IncidentRecord(
            id=incident.id,
            incident=incident,
            state=IncidentState.OPEN,
            tripwires=[event.tripwire],
            pending=Pending(kind="start", trigger="opened"),
            timeline=[TimelineEvent(
                at=now, kind="opened",
                detail={"tripwire": event.tripwire},
            )],
        )
        self._records[record.id] = record
        return incident

    def tick(self) -> None:
        """Drive every incident one step; only segments call the model."""
        for record in self.incidents():
            if record.state == IncidentState.OPEN or (
                record.state == IncidentState.INVESTIGATING
                and record.pending is not None
            ):
                record.state = IncidentState.INVESTIGATING
                self._run_pending(record)
            elif record.state == IncidentState.NEEDS_INPUT:
                self._maybe_resume(record)
            elif record.state == IncidentState.APPLYING:
                self._apply(record)
            elif record.state == IncidentState.VERIFYING:
                self._verify(record)

    def approve(
        self, incident_id: str, proposal_hash: str, *, by: str
    ) -> Any:
        record = self._get(incident_id)
        if record.state != IncidentState.AWAITING_APPROVAL:
            raise ValueError(
                f"incident {incident_id} is {record.state}, "
                "not AWAITING_APPROVAL"
            )
        if not record.proposals or record.proposals[-1].hash != proposal_hash:
            raise ValueError(
                f"proposal {proposal_hash} is not the latest "
                f"for incident {incident_id}"
            )
        approval = self._approvals.approve(incident_id, proposal_hash, by)
        record.state = IncidentState.APPLYING
        record.timeline.append(TimelineEvent(
            at=self._clock.now(), kind="approved",
            detail={"proposal_hash": proposal_hash, "by": by},
        ))
        return approval

    def reject(
        self, incident_id: str, proposal_hash: str, *, by: str, notes: str
    ) -> Any:
        record = self._get(incident_id)
        if record.state != IncidentState.AWAITING_APPROVAL:
            raise ValueError(
                f"incident {incident_id} is {record.state}, "
                "not AWAITING_APPROVAL"
            )
        if not record.proposals or record.proposals[-1].hash != proposal_hash:
            raise ValueError(
                f"proposal {proposal_hash} is not the latest "
                f"for incident {incident_id}"
            )
        rejection = self._approvals.reject(
            incident_id, proposal_hash, by, notes
        )
        record.pending = Pending(
            kind="reopen",
            message=f"Rejected by {by}: {notes} "
            "Revise the proposal or escalate.",
            trigger="rejection",
        )
        record.state = IncidentState.INVESTIGATING
        record.timeline.append(TimelineEvent(
            at=self._clock.now(), kind="rejected",
            detail={"notes": notes, "by": by},
        ))
        return rejection

    def reply(
        self, incident_id: str, message: str, *, by: str, extra_turns: int = 0
    ) -> None:
        record = self._get(incident_id)
        if record.state != IncidentState.NEEDS_INPUT:
            raise ValueError(
                f"incident {incident_id} is {record.state}, not NEEDS_INPUT"
            )
        if not message or not message.strip():
            raise ValueError("reply message must not be empty")
        record.pending = Pending(
            kind="resume",
            message=f"Reply from {by}: {message}",
            extra_turns=extra_turns,
            trigger="reply",
        )
        record.state = IncidentState.INVESTIGATING

    def close(self, incident_id: str, *, by: str, note: str) -> None:
        record = self._get(incident_id)
        if record.state != IncidentState.NEEDS_INPUT:
            raise ValueError(
                f"incident {incident_id} is {record.state}, not NEEDS_INPUT"
            )
        now = self._clock.now()
        record.state = IncidentState.ESCALATED
        record.timeline.append(TimelineEvent(
            at=now, kind="closed", detail={"by": by, "note": note},
        ))
        record.postmortem = build_postmortem(
            record, closed_at=now, outcome="escalated"
        )

    def get(self, incident_id: str) -> IncidentRecord:
        return self._get(incident_id)

    def incidents(self) -> list[IncidentRecord]:
        return sorted(
            self._records.values(),
            key=lambda record: (record.incident.opened_at, record.id),
        )

    def _get(self, incident_id: str) -> IncidentRecord:
        try:
            return self._records[incident_id]
        except KeyError:
            raise ValueError(
                f"unknown incident: {incident_id}"
            ) from None

    def _open_for(self, deployment: str) -> IncidentRecord | None:
        for record in self.incidents():
            if (
                record.incident.deployment == deployment
                and record.state not in _CLOSED
            ):
                return record
        return None

    def _run_pending(self, record: IncidentRecord) -> None:
        pending = record.pending
        assert pending is not None
        if pending.kind in ("resume", "reopen"):
            record.timeline.append(TimelineEvent(
                at=self._clock.now(), kind="resumed",
                detail={
                    "trigger": pending.trigger,
                    "message": pending.message,
                },
            ))
        if pending.kind == "start":
            result = self._investigator.start(record.incident)
        elif pending.kind == "resume":
            result = self._investigator.resume(
                record.incident, message=pending.message,
                extra_turns=pending.extra_turns,
            )
        else:
            result = self._investigator.reopen(
                record.incident, message=pending.message,
                extra_turns=pending.extra_turns,
            )
        record.pending = None
        self._handle_result(record, result)

    def _handle_result(self, record: IncidentRecord, result: Any) -> None:
        now = self._clock.now()
        if result.transcript_id not in record.transcript_ids:
            record.transcript_ids.append(result.transcript_id)
        record.timeline.append(TimelineEvent(
            at=now, kind="segment",
            detail={
                "status": result.status,
                "reason": result.reason,
                "turns": result.turns,
            },
        ))
        if result.status == "submitted":
            diagnosis = Diagnosis.model_validate(
                result.diagnosis
            ).model_dump(mode="json")
            record.diagnosis = diagnosis
            if diagnosis["action"]["type"] == "propose":
                self._propose(record, diagnosis, now)
            else:
                self._begin_verification(
                    record, diagnosis, changed_resource=None
                )
        else:
            self._pause(record, result, now)

    def _propose(
        self, record: IncidentRecord, diagnosis: dict[str, Any], now: Any
    ) -> None:
        action = diagnosis["action"]
        method = action["method"]
        resource = action["resource"]
        update_mask = list(action.get("updateMask") or [])
        patch = dict(action.get("patch") or {})
        version = resource_version(self._reader.get(resource))
        seq = len(record.proposals) + 1
        digest = proposal_hash(
            record.id, seq, method, resource, update_mask, patch,
            version,
        )
        record.proposals.append(Proposal(
            incident_id=record.id,
            seq=seq,
            hash=digest,
            method=method,
            resource=resource,
            update_mask=update_mask,
            patch=patch,
            resource_version=version,
            diagnosis=diagnosis,
            created_at=now,
        ))
        record.state = IncidentState.AWAITING_APPROVAL
        record.timeline.append(TimelineEvent(
            at=now, kind="proposal",
            detail={"hash": digest, "seq": seq},
        ))

    def _begin_verification(
        self,
        record: IncidentRecord,
        diagnosis: dict[str, Any],
        changed_resource: str | None,
    ) -> None:
        now = self._clock.now()
        self._verifier.start(
            record.incident, diagnosis, changed_resource=changed_resource
        )
        expected = diagnosis["expectedVerification"]
        self._verifying[record.id] = {
            "deployment": record.incident.deployment,
            "tripwire": expected["tripwire"],
            "seconds": expected["seconds"],
            "started_at": _iso_z(now),
        }
        record.state = IncidentState.VERIFYING
        record.timeline.append(TimelineEvent(
            at=now, kind="verification_started",
            detail={
                "tripwire": expected["tripwire"],
                "seconds": expected["seconds"],
            },
        ))

    def _pause(self, record: IncidentRecord, result: Any, now: Any) -> None:
        statuses = self._evaluator.status()[record.incident.deployment]
        record.state = IncidentState.NEEDS_INPUT
        record.reason = result.reason
        record.paused_at = now
        record.paused_snapshot = {
            tripwire: statuses[tripwire] for tripwire in record.tripwires
        }
        if result.reason == "escalated":
            record.diagnosis = Diagnosis.model_validate(
                result.diagnosis
            ).model_dump(mode="json")
        record.timeline.append(TimelineEvent(
            at=now, kind="needs_input",
            detail={"reason": result.reason},
        ))

    def _maybe_resume(self, record: IncidentRecord) -> None:
        statuses = self._evaluator.status()[record.incident.deployment]
        current = {
            tripwire: statuses[tripwire] for tripwire in record.tripwires
        }
        snapshot = record.paused_snapshot or {}
        if not any(
            old == "tripped" and current.get(tripwire) == "ok"
            for tripwire, old in snapshot.items()
        ):
            return
        assert record.paused_at is not None
        # A tripwire attached since the pause reports "none -> tripped"
        # (pin 13): attach only happens on a trip, so the trip is the
        # change being reported even if it recovered before the resume.
        # Current state follows in the "Current:" list.
        parts = []
        for tripwire in record.tripwires:
            old = snapshot.get(tripwire, "none")
            if tripwire not in snapshot:
                parts.append(f"{tripwire} none -> tripped")
            elif old != current[tripwire]:
                parts.append(
                    f"{tripwire} {old} -> {current[tripwire]}"
                )
        changes = "; ".join(parts)
        current_text = "; ".join(
            f"{tripwire} {current[tripwire]}"
            for tripwire in record.tripwires
        )
        record.pending = Pending(
            kind="resume",
            message="Tripwire state changed since the pause at "
            f"{_iso_z(record.paused_at)}: {changes}. "
            f"Current: {current_text}.",
            trigger="tripwire",
        )
        record.state = IncidentState.INVESTIGATING
        self._run_pending(record)

    def _apply(self, record: IncidentRecord) -> None:
        now = self._clock.now()
        proposal = record.proposals[-1]
        try:
            self._executor.apply(record.incident, proposal)
        except NotApproved:
            raise
        except Conflict as exc:
            record.pending = Pending(
                kind="reopen",
                message=f"Conflict: {exc.resource} changed since "
                f"the proposal (version {exc.expected} -> "
                f"{exc.actual}). Read it again before proposing.",
                trigger="conflict",
            )
            record.state = IncidentState.INVESTIGATING
            record.timeline.append(TimelineEvent(
                at=now, kind="conflict",
                detail={
                    "resource": exc.resource,
                    "expected": exc.expected,
                    "actual": exc.actual,
                },
            ))
        except InvalidPatch as exc:
            record.pending = Pending(
                kind="reopen",
                message=f"Apply refused: {'; '.join(exc.problems)}.",
                trigger="invalid_patch",
            )
            record.state = IncidentState.INVESTIGATING
        except ApplyError as exc:
            record.failed_attempts += 1
            record.timeline.append(TimelineEvent(
                at=now, kind="apply_failed",
                detail={"detail": exc.detail},
            ))
            if record.failed_attempts >= 2:
                self._escalate(record, now)
            else:
                record.pending = Pending(
                    kind="reopen",
                    message=f"Apply failed: {exc.detail}.",
                    trigger="apply_failed",
                )
                record.state = IncidentState.INVESTIGATING
        else:
            approval = self._approvals.approval_for(proposal.hash)
            assert approval is not None
            self._state.record_change(record.incident.deployment, {
                "at": now.isoformat(),
                "method": proposal.method,
                "resource": proposal.resource,
                "updateMask": proposal.update_mask,
                "patch": proposal.patch,
                "proposal_hash": proposal.hash,
                "approver": approval.approver,
            })
            assert record.diagnosis is not None
            record.timeline.append(TimelineEvent(
                at=now, kind="applied",
                detail={"proposal_hash": proposal.hash},
            ))
            self._begin_verification(
                record, record.diagnosis,
                changed_resource=proposal.resource,
            )

    def _verify(self, record: IncidentRecord) -> None:
        now = self._clock.now()
        outcome = self._verifier.check(record.id)
        if outcome == "verified":
            record.state = IncidentState.RESOLVED
            record.timeline.append(TimelineEvent(
                at=now, kind="verified", detail={},
            ))
            record.postmortem = build_postmortem(
                record, closed_at=now, outcome="resolved"
            )
        elif outcome == "failed":
            record.failed_attempts += 1
            record.timeline.append(TimelineEvent(
                at=now, kind="verification_failed", detail={},
            ))
            if record.failed_attempts >= 2:
                self._escalate(record, now)
            else:
                context = self._verifying[record.id]
                record.pending = Pending(
                    kind="reopen",
                    message=f"Verification failed: {context['tripwire']} "
                    f"on {context['deployment']} was not ok within "
                    f"{context['seconds']} s of "
                    f"{context['started_at']}. Investigate again.",
                    trigger="verification_failed",
                )
                record.state = IncidentState.INVESTIGATING

    def _escalate(self, record: IncidentRecord, now: Any) -> None:
        record.state = IncidentState.ESCALATED
        record.timeline.append(TimelineEvent(
            at=now, kind="escalated",
            detail={"failed_attempts": record.failed_attempts},
        ))
        record.postmortem = build_postmortem(
            record, closed_at=now, outcome="escalated"
        )
