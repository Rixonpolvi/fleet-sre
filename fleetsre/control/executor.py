"""Journaled executor: approved writes with crash recovery (pins 5, 18, 19)."""

import copy
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

from fleetsre.control.proposals import resource_version
from fleetsre.ports import ChronicleError


class NotApproved(Exception):
    pass


class Conflict(Exception):
    def __init__(self, resource: str, expected: str, actual: str) -> None:
        super().__init__(
            f"conflict on {resource}: expected {expected}, got {actual}"
        )
        self.resource = resource
        self.expected = expected
        self.actual = actual


class InvalidPatch(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"invalid patch: {'; '.join(problems)}")
        self.problems = problems


class ApplyError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"apply failed: {detail}")
        self.detail = detail


class ApplyResult(BaseModel):
    proposal_hash: str
    outcome: Literal["applied", "already_applied"]
    api_calls: int
    resource_after: dict[str, Any]


def _iso_z(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _is_connector(resource: str) -> bool:
    return "/connectorInstances/" in resource


class Executor:
    """Applies approved proposals; reconciles crashed intents (pins 18, 19)."""

    def __init__(
        self,
        chronicle: Any,
        approvals: Any,
        journal: Any,
        *,
        clock: Any,
    ) -> None:
        if chronicle.identity != "deployer":
            raise ValueError(
                "executor needs the deployer identity, "
                f"got {chronicle.identity!r}"
            )
        self._chronicle = chronicle
        self._approvals = approvals
        self._journal = journal
        self._clock = clock

    def apply(self, incident: Any, proposal: Any) -> ApplyResult:
        """Apply one approved proposal, journaling intent first (pin 18)."""
        approval = self._approvals.approval_for(proposal.hash)
        if approval is None or approval.incident_id != incident.id:
            raise NotApproved(
                f"no approval for proposal {proposal.hash} "
                f"on incident {incident.id}"
            )
        current = self._chronicle.get(proposal.resource)
        actual = resource_version(current)
        if actual != proposal.resource_version:
            raise Conflict(proposal.resource, proposal.resource_version, actual)
        steps = self._steps(proposal, current)
        self._journal.append({
            "type": "intent",
            "proposal_hash": proposal.hash,
            "approval_id": approval.id,
            "incident_id": incident.id,
            "method": proposal.method,
            "resource": proposal.resource,
            "resource_version": proposal.resource_version,
            "steps": steps,
            "at": _iso_z(self._clock.now()),
        })
        self._journal.append({
            "type": "started",
            "proposal_hash": proposal.hash,
            "at": _iso_z(self._clock.now()),
        })
        calls = 0
        for index, step in enumerate(steps):
            try:
                self._run_step(proposal.resource, step)
            except ChronicleError as exc:
                detail = str(exc)
                self._journal.append({
                    "type": "terminal",
                    "proposal_hash": proposal.hash,
                    "outcome": "error",
                    "api_calls": calls,
                    "detail": detail,
                    "at": _iso_z(self._clock.now()),
                })
                raise ApplyError(detail) from exc
            calls += 1
            self._journal.append({
                "type": "step",
                "proposal_hash": proposal.hash,
                "index": index,
                "at": _iso_z(self._clock.now()),
            })
        self._journal.append({
            "type": "terminal",
            "proposal_hash": proposal.hash,
            "outcome": "applied",
            "api_calls": calls,
            "detail": "",
            "at": _iso_z(self._clock.now()),
        })
        return ApplyResult(
            proposal_hash=proposal.hash,
            outcome="applied",
            api_calls=calls,
            resource_after=self._chronicle.get(proposal.resource),
        )

    def recover(self) -> list[ApplyResult]:
        """Reconcile every intent with no terminal record (pin 19)."""
        terminal = {
            record.get("proposal_hash")
            for record in self._journal.records()
            if record.get("type") == "terminal"
        }
        intents = [
            record for record in self._journal.records()
            if record.get("type") == "intent"
            and record.get("proposal_hash") not in terminal
        ]
        return [self._recover_one(intent) for intent in intents]

    def _recover_one(self, intent: dict[str, Any]) -> ApplyResult:
        proposal_hash = str(intent["proposal_hash"])
        method = str(intent["method"])
        resource = str(intent["resource"])
        fresh = self._chronicle.get(resource)
        if method.endswith(".patch"):
            if self._patch_done(intent, fresh):
                return self._terminal_already(proposal_hash, fresh)
            steps = self._refresh(intent["steps"], fresh)
            return self._finish(intent, resource, fresh, steps)
        if method.endswith(".restart"):
            moved = resource_version(fresh) != intent["resource_version"]
            if fresh.get("enabled") is False:
                steps = [self._enabled_step(resource, fresh, True)]
                return self._finish(
                    intent, resource, fresh, steps, indices=[1]
                )
            if not moved:
                steps = [
                    self._enabled_step(resource, fresh, False),
                    self._enabled_step(resource, fresh, True),
                ]
                return self._finish(
                    intent, resource, fresh, steps, indices=[0, 1]
                )
            return self._terminal_already(proposal_hash, fresh)
        if method.endswith(".runOnDemand"):
            if _is_connector(resource):
                body: dict[str, Any] = {"connectorInstance": fresh}
            else:
                body = {}
            steps = [{
                "call": "run_on_demand", "update_mask": [], "body": body,
            }]
            return self._finish(intent, resource, fresh, steps)
        raise ValueError(f"unknown method in intent: {method!r}")

    def _finish(
        self,
        intent: dict[str, Any],
        resource: str,
        fresh: dict[str, Any],
        steps: list[dict[str, Any]],
        indices: list[int] | None = None,
    ) -> ApplyResult:
        proposal_hash = str(intent["proposal_hash"])
        self._journal.append({
            "type": "started",
            "proposal_hash": proposal_hash,
            "at": _iso_z(self._clock.now()),
        })
        order = indices if indices is not None else list(range(len(steps)))
        calls = 0
        for step, index in zip(steps, order):
            try:
                self._run_step(resource, step)
            except ChronicleError as exc:
                detail = str(exc)
                self._journal.append({
                    "type": "terminal",
                    "proposal_hash": proposal_hash,
                    "outcome": "error",
                    "api_calls": calls,
                    "detail": detail,
                    "at": _iso_z(self._clock.now()),
                })
                raise ApplyError(detail) from exc
            calls += 1
            self._journal.append({
                "type": "step",
                "proposal_hash": proposal_hash,
                "index": index,
                "at": _iso_z(self._clock.now()),
            })
        self._journal.append({
            "type": "terminal",
            "proposal_hash": proposal_hash,
            "outcome": "applied",
            "api_calls": calls,
            "detail": "",
            "at": _iso_z(self._clock.now()),
        })
        return ApplyResult(
            proposal_hash=proposal_hash,
            outcome="applied",
            api_calls=calls,
            resource_after=self._chronicle.get(resource),
        )

    def _terminal_already(
        self, proposal_hash: str, fresh: dict[str, Any]
    ) -> ApplyResult:
        self._journal.append({
            "type": "terminal",
            "proposal_hash": proposal_hash,
            "outcome": "already_applied",
            "api_calls": 0,
            "detail": "",
            "at": _iso_z(self._clock.now()),
        })
        return ApplyResult(
            proposal_hash=proposal_hash,
            outcome="already_applied",
            api_calls=0,
            resource_after=fresh,
        )

    def _patch_done(
        self, intent: dict[str, Any], fresh: dict[str, Any]
    ) -> bool:
        moved = resource_version(fresh) != intent["resource_version"]
        for step in intent["steps"]:
            body = step["body"]
            for field in step["update_mask"]:
                if field == "parameters":
                    if not self._params_done(
                        body["parameters"], fresh, moved
                    ):
                        return False
                elif fresh.get(field) != body.get(field):
                    return False
        return True

    def _params_done(
        self,
        proposed: list[dict[str, Any]],
        fresh: dict[str, Any],
        moved: bool,
    ) -> bool:
        current = {
            param.get("displayName"): param
            for param in fresh.get("parameters", [])
        }
        for param in proposed:
            display = param.get("displayName")
            have = current.get(display)
            if have is None:
                return False
            if have.get("type") == "PASSWORD":
                if not moved:
                    return False
            elif str(have.get("value")) != str(param.get("value")):
                return False
        return True

    def _steps(
        self, proposal: Any, current: dict[str, Any]
    ) -> list[dict[str, Any]]:
        method = proposal.method
        if method in ("connectorInstances.patch", "jobInstances.patch"):
            return [{
                "call": "patch",
                "update_mask": list(proposal.update_mask),
                "body": self._patch_body(proposal, current),
            }]
        if method in ("connectorInstances.restart", "jobInstances.restart"):
            return [
                self._restart_step(proposal, current, False),
                self._restart_step(proposal, current, True),
            ]
        if method == "connectorInstances.runOnDemand":
            return [{
                "call": "run_on_demand",
                "update_mask": [],
                "body": {"connectorInstance": current},
            }]
        if method == "jobInstances.runOnDemand":
            return [{
                "call": "run_on_demand", "update_mask": [], "body": {},
            }]
        raise InvalidPatch([f"unknown method: {method}"])

    def _patch_body(
        self, proposal: Any, current: dict[str, Any]
    ) -> dict[str, Any]:
        patch = proposal.patch or {}
        body: dict[str, Any] = {}
        for field in proposal.update_mask:
            if field == "parameters":
                proposed = patch.get("parameters")
                if not isinstance(proposed, list):
                    raise InvalidPatch([
                        "parameters is in updateMask "
                        "but missing from the patch"
                    ])
                body["parameters"] = self._merge_params(
                    proposed, current.get("parameters", [])
                )
            elif field not in patch:
                raise InvalidPatch([
                    f"{field} is in updateMask but missing from the patch"
                ])
            else:
                body[field] = patch[field]
        if _is_connector(proposal.resource):
            body["integrationVersion"] = current["integrationVersion"]
        return body

    def _merge_params(
        self,
        proposed: list[dict[str, Any]],
        current: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        have = {param.get("displayName") for param in current}
        updates = {}
        for param in proposed:
            display = param.get("displayName")
            if display not in have:
                raise InvalidPatch([f"unknown parameter: {display}"])
            updates[display] = param.get("value", "")
        merged = []
        for param in current:
            entry = dict(param)
            if entry.get("displayName") in updates:
                entry["value"] = updates[entry["displayName"]]
            merged.append(entry)
        return merged

    def _restart_step(
        self, proposal: Any, current: dict[str, Any], enabled: bool
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"enabled": enabled}
        if _is_connector(proposal.resource):
            body["integrationVersion"] = current["integrationVersion"]
        return {"call": "patch", "update_mask": ["enabled"], "body": body}

    def _enabled_step(
        self, resource: str, fresh: dict[str, Any], enabled: bool
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"enabled": enabled}
        if _is_connector(resource):
            body["integrationVersion"] = fresh["integrationVersion"]
        return {"call": "patch", "update_mask": ["enabled"], "body": body}

    def _refresh(
        self, steps: list[dict[str, Any]], fresh: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Re-run bodies with integrationVersion from the fresh read."""
        resource = fresh.get("name", "")
        remade = copy.deepcopy(steps)
        for step in remade:
            if step.get("call") == "patch" and _is_connector(str(resource)):
                step["body"]["integrationVersion"] = fresh[
                    "integrationVersion"
                ]
        return remade

    def _run_step(self, resource: str, step: dict[str, Any]) -> None:
        if step["call"] == "patch":
            self._chronicle.patch(
                resource, step["body"], list(step["update_mask"])
            )
        elif step["call"] == "run_on_demand":
            self._chronicle.run_on_demand(resource, step["body"])
        else:
            raise ValueError(f"unknown step call: {step['call']!r}")
