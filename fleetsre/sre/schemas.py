"""SRE schemas: hypotheses, evidence, diagnosis, incident (spec.md §6)."""

import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_serializer


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
    model_config = ConfigDict(extra="forbid")

    hypothesis: str = ""
    why: str = ""


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["propose", "none", "escalate"]
    method: str | None = None
    resource: str | None = None
    updateMask: list[str] | None = None
    patch: dict[str, Any] | None = None
    escalationSummary: str | None = None


class ExpectedVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tripwire: str = ""
    condition: str = ""
    seconds: int = 0


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    deployment: str
    tripwire: str
    opened_at: datetime
    trip_event: Any
    service: str = ""
    component: str = ""
    resources: dict[str, str] = {}
    siblings: list[str] = []

    @field_serializer("trip_event")
    def _ser_trip_event(self, value: Any) -> Any:
        if isinstance(value, dict):
            return value
        at = getattr(value, "at", None)
        return {
            "kind": getattr(value, "kind", "trip"),
            "deployment": getattr(value, "deployment", ""),
            "tripwire": getattr(value, "tripwire", ""),
            "at": at.isoformat() if at is not None else "",
            "reason": getattr(value, "reason", ""),
            "evidence": list(getattr(value, "evidence", ())),
        }

    @classmethod
    def from_trip(
        cls, event: Any, registry: Any, now: datetime
    ) -> "Incident":
        """Open an incident from a trip event (docs/d5-pins.md pin 6)."""
        deployment = str(event.deployment)
        tripwire_id = str(event.tripwire)
        service, _, environment = deployment.partition("/")
        at = event.at
        if isinstance(at, datetime) and at.tzinfo is not None:
            at_utc = at.astimezone(timezone.utc)
        else:
            at_utc = at
        incident_id = (
            f"inc-{service}-{environment}-{tripwire_id}-"
            f"{at_utc:%Y%m%dT%H%M%SZ}"
        )
        component = ""
        resources: dict[str, str] = {}
        siblings: list[str] = []
        try:
            contract = registry.contract(service)
        except KeyError:
            contract = None
        if contract is not None:
            for tripwire in contract.tripwires:
                if tripwire.id == tripwire_id:
                    component = tripwire.component
                    break
            try:
                dep = registry.deployment(deployment)
            except KeyError:
                dep = None
            if dep is not None:
                resources = dict(dep.resource_names)
            siblings = [
                d.id for d in registry.deployments(service)
                if d.id != deployment
            ]
        return cls(
            id=incident_id,
            deployment=deployment,
            tripwire=tripwire_id,
            opened_at=now,
            trip_event=event,
            service=service,
            component=component,
            resources=resources,
            siblings=siblings,
        )

    @classmethod
    def reconstruct(
        cls, incident_id: str, registry: Any, now: datetime
    ) -> "Incident":
        """Rebuild incident context from an incident ID and the registry.

        The ToolSet reaches the gates through the session, which carries
        only the incident ID; the ID embeds service, environment,
        tripwire and trip time (pin 6), and the registry supplies the
        rest. Parsing matches the registry's known services,
        environments and tripwire IDs (longest first); it never splits
        on "-" because those names contain hyphens. Raises ValueError
        when the ID does not parse.
        """
        match = re.fullmatch(
            r"inc-(.+)-(\d{8}T\d{6}Z)", incident_id
        )
        if match is None:
            raise ValueError(f"bad incident id: {incident_id!r}")
        head, stamp = match.group(1), match.group(2)
        try:
            at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            raise ValueError(f"bad incident id: {incident_id!r}") from None
        found: tuple[str, str, str] | None = None
        services = sorted(registry.services, key=len, reverse=True)
        for service in services:
            prefix = f"{service}-"
            if not head.startswith(prefix):
                continue
            rest = head[len(prefix):]
            try:
                contract = registry.contract(service)
            except KeyError:
                continue
            tripwire_ids = {t.id for t in contract.tripwires}
            envs = sorted(
                {d.environment for d in registry.deployments(service)},
                key=len, reverse=True,
            )
            for env in envs:
                env_prefix = f"{env}-"
                if not rest.startswith(env_prefix):
                    continue
                tripwire_id = rest[len(env_prefix):]
                if tripwire_id in tripwire_ids:
                    found = (service, env, tripwire_id)
                    break
            if found is not None:
                break
        if found is None:
            raise ValueError(f"bad incident id: {incident_id!r}")
        service, environment, tripwire_id = found
        deployment = f"{service}/{environment}"
        try:
            registry.deployment(deployment)
        except KeyError:
            raise ValueError(f"bad incident id: {incident_id!r}") from None
        event = SimpleNamespace(
            kind="trip",
            deployment=deployment,
            tripwire=tripwire_id,
            at=at,
            reason="",
            evidence=(),
        )
        return cls.from_trip(event, registry, now)


def _resolve_param(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return params.get(value[2:-1], value)
    if isinstance(value, dict):
        return {k: _resolve_param(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_param(v, params) for v in value]
    return value


def render_incident(incident: Incident, registry: Any) -> str:
    """Build the first user message for an incident (d5-pins.md pin 6)."""
    contract = registry.contract(incident.service)
    deployment = registry.deployment(incident.deployment)
    tripwire = next(
        t for t in contract.tripwires if t.id == incident.tripwire
    )
    spec = _resolve_param(dict(tripwire.spec), dict(deployment.parameters))
    event = incident.trip_event
    if isinstance(event, dict):
        reason = event.get("reason", "")
        evidence = list(event.get("evidence", []))
        tripped_at = event.get("at", "")
    else:
        reason = getattr(event, "reason", "")
        evidence = list(getattr(event, "evidence", ()))
        at = getattr(event, "at", "")
        tripped_at = at.isoformat() if isinstance(at, datetime) else str(at)
    lines = [
        f"Incident {incident.id}",
        f"Deployment: {incident.deployment}",
        f"Tripwire: {incident.tripwire}",
        f"Component: {incident.component} "
        "(the tripped tripwire's component)",
        f"Tripwire type: {tripwire.type}",
        f"Tripwire spec: {json.dumps(spec, sort_keys=True)}",
        f"Trip reason: {reason}",
        "Trip evidence insertIds: "
        + (", ".join(evidence) if evidence else "none"),
        f"Tripped at: {tripped_at}",
        f"Now: {incident.opened_at.isoformat()}",
        "Resource names:",
        *[
            f"- {comp}: {name}"
            for comp, name in sorted(incident.resources.items())
        ],
        "Components:",
        *[
            f"- {comp.id} ({comp.kind}): "
            f"{incident.resources.get(comp.id, 'none')}"
            for comp in contract.components
        ],
        "Tripwires:",
        *[
            f"- {tw.id} ({tw.component})"
            for tw in contract.tripwires
        ],
        "Sibling deployments: "
        + (", ".join(incident.siblings) if incident.siblings else "none"),
        "Budgets: 24 turns, 12 read calls, 400000 tokens per segment.",
    ]
    return "\n".join(lines)


READ_TOOL_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "query_logs": {
        "type": "object",
        "properties": {
            "entries": {"type": "array"},
            "total": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
        "required": ["entries", "total", "truncated"],
        "additionalProperties": False,
    },
    "get_resource": {"type": "object"},
    "list_connector_logs": {
        "type": "object",
        "properties": {
            "logs": {"type": "array"},
            "truncated": {"type": "boolean"},
        },
        "required": ["logs", "truncated"],
        "additionalProperties": False,
    },
    "list_job_logs": {
        "type": "object",
        "properties": {
            "logs": {"type": "array"},
            "truncated": {"type": "boolean"},
        },
        "required": ["logs", "truncated"],
        "additionalProperties": False,
    },
    "test_integration_instance": {"type": "object"},
    "compare_siblings": {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "component": {"type": "string"},
            "window": {"type": "object"},
            "deployments": {"type": "object"},
        },
        "required": ["service", "component", "window", "deployments"],
        "additionalProperties": False,
    },
    "tripwire_history": {
        "type": "object",
        "properties": {
            "deployment": {"type": "string"},
            "events": {"type": "array"},
        },
        "required": ["deployment", "events"],
        "additionalProperties": False,
    },
    "get_contract": {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "contract": {"type": "object"},
            "scorecard": {"type": ["object", "null"]},
        },
        "required": ["service", "contract", "scorecard"],
        "additionalProperties": False,
    },
}
