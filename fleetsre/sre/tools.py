"""ToolSet: read tools (§6.2) plus working tools (§6.3) with gates inside."""

import copy
import re
from datetime import datetime, timezone
from typing import Any

import jsonschema

from fleetsre.ports import ChronicleError
from fleetsre.registry import CHRONICLE_PREFIX
from fleetsre.sre.gates import (
    check_diagnosis,
    check_evidence,
    check_hypotheses,
)
from fleetsre.sre.schemas import Evidence, Hypothesis, Incident
from fleetsre.sre.session import CallRecord

READ_TOOLS = frozenset({
    "query_logs",
    "get_resource",
    "list_connector_logs",
    "list_job_logs",
    "test_integration_instance",
    "compare_siblings",
    "tripwire_history",
    "get_contract",
})

_SEVERITIES = [
    "DEBUG", "INFO", "NOTICE", "WARNING",
    "ERROR", "CRITICAL", "ALERT", "EMERGENCY",
]

_SEV_RANK = {name: rank for rank, name in enumerate(_SEVERITIES)}

_RATIONALE = {
    "type": "string",
    "maxLength": 200,
    "description": "Visible think line: what this call should learn.",
}

_WINDOW = {
    "type": "object",
    "properties": {
        "lastMinutes": {"type": "integer", "minimum": 1, "maximum": 360},
    },
    "required": ["lastMinutes"],
    "additionalProperties": False,
}

_DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "incidentId": {"type": "string"},
        "deployment": {"type": "string"},
        "component": {"type": "string"},
        "category": {
            "type": "string",
            "enum": [
                "connector_stalled", "job_stalled", "credential_failure",
                "schema_drift", "job_hollow_success", "job_failing",
                "unknown",
            ],
        },
        "hypothesis": {"type": "string"},
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "openQuestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["hypothesis", "why"],
                "additionalProperties": False,
            },
        },
        "action": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["propose", "none", "escalate"],
                },
                "method": {"type": "string"},
                "resource": {"type": "string"},
                "updateMask": {
                    "type": "array", "items": {"type": "string"},
                },
                "patch": {"type": "object"},
                "escalationSummary": {"type": "string"},
            },
            "required": ["type"],
            "additionalProperties": False,
        },
        "expectedVerification": {
            "type": "object",
            "properties": {
                "tripwire": {"type": "string"},
                "condition": {
                    "type": "string", "enum": ["recovers_within"],
                },
                "seconds": {"type": "integer"},
            },
            "required": ["tripwire", "condition", "seconds"],
            "additionalProperties": False,
        },
        "rollback": {"type": "string"},
        "confidence": {
            "type": "string", "enum": ["low", "medium", "high"],
        },
    },
    "required": [
        "incidentId", "deployment", "component", "category", "hypothesis",
        "summary", "evidence", "action", "confidence",
    ],
    "additionalProperties": False,
}


def _spec(
    name: str, description: str, properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_SPECS: list[dict[str, Any]] = [
    _spec(
        "query_logs", "Query log entries for a deployment over a window.",
        {
            "deployment": {"type": "string"},
            "window": _WINDOW,
            "filter": {
                "type": "object",
                "properties": {
                    "component": {"type": "string"},
                    "event": {"type": "string"},
                    "minSeverity": {"type": "string", "enum": _SEVERITIES},
                    "runId": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "rationale": _RATIONALE,
        },
        ["deployment", "window", "filter", "rationale"],
    ),
    _spec(
        "get_resource", "Get a Chronicle resource by full name (GET only).",
        {"name": {"type": "string"}, "rationale": _RATIONALE},
        ["name", "rationale"],
    ),
    _spec(
        "list_connector_logs",
        "List ConnectorLog records for a connector instance.",
        {"name": {"type": "string"}, "rationale": _RATIONALE},
        ["name", "rationale"],
    ),
    _spec(
        "list_job_logs", "List JobInstanceLog records for a job instance.",
        {"name": {"type": "string"}, "rationale": _RATIONALE},
        ["name", "rationale"],
    ),
    _spec(
        "test_integration_instance",
        "Run a connectivity test (changes no state).",
        {"name": {"type": "string"}, "rationale": _RATIONALE},
        ["name", "rationale"],
    ),
    _spec(
        "compare_siblings",
        "Compare one component's signals across every deployment.",
        {
            "service": {"type": "string"},
            "component": {"type": "string"},
            "window": _WINDOW,
            "rationale": _RATIONALE,
        },
        ["service", "component", "window", "rationale"],
    ),
    _spec(
        "tripwire_history", "Recent trips, recoveries and approved changes.",
        {"deployment": {"type": "string"}, "rationale": _RATIONALE},
        ["deployment", "rationale"],
    ),
    _spec(
        "get_contract", "Current contract and last scorecard summary.",
        {"service": {"type": "string"}, "rationale": _RATIONALE},
        ["service", "rationale"],
    ),
    _spec(
        "set_hypotheses", "Set 2-4 hypotheses with an alternative.",
        {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "statement": {"type": "string"},
                        "wouldConfirm": {"type": "string"},
                        "alternative": {"type": "boolean"},
                    },
                    "required": [
                        "id", "statement", "wouldConfirm", "alternative",
                    ],
                    "additionalProperties": False,
                },
            },
            "rationale": _RATIONALE,
        },
        ["items", "rationale"],
    ),
    _spec(
        "update_plan", "Record how each hypothesis will be settled.",
        {
            "plan": {"type": "array"},
            "rationale": _RATIONALE,
        },
        ["plan", "rationale"],
    ),
    _spec(
        "record_evidence", "Record one supports/refutes item.",
        {
            "hypothesis": {"type": "string"},
            "stance": {"type": "string", "enum": ["supports", "refutes"]},
            "source": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"insertId": {"type": "string"}},
                        "required": ["insertId"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "callId": {"type": "string"},
                            "field": {"type": "string"},
                            "value": True,
                        },
                        "required": ["callId", "field", "value"],
                        "additionalProperties": False,
                    },
                ],
            },
            "why": {"type": "string"},
            "rationale": _RATIONALE,
        },
        ["hypothesis", "stance", "source", "why", "rationale"],
    ),
    _spec(
        "submit_diagnosis", "Submit the diagnosis for the incident.",
        {
            "diagnosis": _DIAGNOSIS_SCHEMA,
            "rationale": _RATIONALE,
        },
        ["diagnosis", "rationale"],
    ),
]

_SPEC_BY_NAME = {spec["name"]: spec for spec in _SPECS}

_PREFIX = re.escape(CHRONICLE_PREFIX)
_SEG = r"[^/:]+"  # path segments never contain : (custom methods) or /
_NAME_PATTERNS = {
    "any": re.compile(
        rf"(?:{_PREFIX}/environments/{_SEG}"
        rf"|{_PREFIX}/integrations/{_SEG}/integrationInstances/{_SEG}"
        rf"|{_PREFIX}/integrations/{_SEG}/connectors/{_SEG}"
        rf"/connectorInstances/{_SEG}"
        rf"|{_PREFIX}/integrations/{_SEG}/jobs/{_SEG}/jobInstances/{_SEG})"
    ),
    "connector": re.compile(
        rf"{_PREFIX}/integrations/{_SEG}/connectors/{_SEG}"
        rf"/connectorInstances/{_SEG}"
    ),
    "job": re.compile(
        rf"{_PREFIX}/integrations/{_SEG}/jobs/{_SEG}/jobInstances/{_SEG}"
    ),
    "integration": re.compile(
        rf"{_PREFIX}/integrations/{_SEG}/integrationInstances/{_SEG}"
    ),
}


def _misnested_hint(
    spec: dict[str, Any], path: list[Any], exc: Any
) -> str | None:
    """Hint for expectedVerification nested inside action (live mistake).

    Returns the full diagnosis.schema problem, or None when the error
    is anything else.
    """
    if exc.validator != "additionalProperties" or path != [
        "diagnosis", "action",
    ]:
        return None
    try:
        action_spec = spec["parameters"]["properties"]["diagnosis"][
            "properties"
        ]["action"]
        allowed = set(action_spec["properties"])
        extras = sorted(set(exc.instance) - allowed)
    except (KeyError, TypeError):
        return None
    if extras != ["expectedVerification"]:
        return None
    return (
        "diagnosis.schema: action: additional property "
        "'expectedVerification' (it belongs beside action)"
    )


def _iso_z(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


class ToolSet:
    """Read tools plus gated working tools (spec.md §6.2-§6.3)."""

    def __init__(
        self,
        log_query: Any,
        chronicle: Any,
        state: Any,
        registry: Any,
        *,
        clock: Any,
        verification: bool = True,
    ) -> None:
        self.log_query = log_query
        self.chronicle = chronicle
        self.state = state
        self.registry = registry
        self.clock = clock
        self.verification = verification
        self._local = 0

    def specs(self) -> list[dict[str, Any]]:
        return copy.deepcopy(_SPECS)

    def call(
        self,
        name: str,
        args: dict[str, Any],
        session: Any,
        *,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one tool call; refusals return error results, never raise."""
        if call_id is None:
            call_id = self._fresh_id(session)
        try:
            result, verdict, problems = self._dispatch(name, args, session)
        except Exception as exc:
            result = {"error": f"{name} failed: {exc}"}
            verdict = "refused"
            problems = [f"tool.error: internal error in {name}: {exc}"]
            result["problems"] = problems
        session.calls[call_id] = CallRecord(
            tool=name,
            args=args if isinstance(args, dict) else {},
            result=result,
            verdict=verdict,
            problems=problems,
            turn=session.turn,
        )
        return {"callId": call_id, "result": result}

    def _fresh_id(self, session: Any) -> str:
        calls = getattr(session, "calls", {})
        while True:
            self._local += 1
            candidate = f"local-{self._local}"
            if candidate not in calls:
                return candidate

    def _dispatch(
        self, name: str, args: Any, session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        spec = _SPEC_BY_NAME.get(name)
        if spec is None:
            problems = [f"tool.unknown: unknown tool: {name}"]
            return {"error": f"{name} refused", "problems": problems}, \
                "refused", problems
        if not isinstance(args, dict):
            problems = ["args.schema: arguments must be an object"]
            return {"error": f"{name} refused", "problems": problems}, \
                "refused", problems
        if (
            name == "submit_diagnosis"
            and self.verification
            and not getattr(session, "hypotheses", None)
        ):
            # With gates on, a submission before any hypotheses reports
            # the order violation first, ahead of argument validation.
            problems = [
                "order: set_hypotheses must be accepted before "
                "submit_diagnosis"
            ]
            return {"error": f"{name} refused", "problems": problems}, \
                "refused", problems
        try:
            jsonschema.Draft202012Validator(
                spec["parameters"]
            ).validate(args)
        except jsonschema.ValidationError as exc:
            path = list(exc.absolute_path)
            if name == "submit_diagnosis" and path[:1] == ["diagnosis"]:
                hint = _misnested_hint(spec, path, exc)
                if hint is not None:
                    problems = [hint]
                else:
                    disp = ".".join(str(p) for p in path[1:]) or "(root)"
                    problems = [f"diagnosis.schema: {disp}: {exc.message}"]
            else:
                disp = ".".join(str(p) for p in path) or "(root)"
                problems = [f"args.schema: {disp}: {exc.message}"]
            return {"error": f"{name} refused", "problems": problems}, \
                "refused", problems
        handler = getattr(self, f"_tool_{name}", None)
        assert handler is not None, f"missing handler for {name}"
        return handler(args, session)

    def _incident_for(self, session: Any) -> Incident:
        now = self.clock.now()
        try:
            return Incident.reconstruct(
                str(getattr(session, "incident_id", "")), self.registry, now
            )
        except ValueError:
            return Incident(
                id=str(getattr(session, "incident_id", "")),
                deployment="",
                tripwire="",
                opened_at=now,
                trip_event=None,
                service="",
                component="",
                resources={},
                siblings=[],
            )

    # -- read tools ----------------------------------------------------

    def _tool_query_logs(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        from datetime import timedelta

        deployment = args["deployment"]
        minutes = int(args["window"]["lastMinutes"])
        filt = args.get("filter", {})
        want_comp = filt.get("component")
        if want_comp is not None:
            service = deployment.split("/", 1)[0]
            try:
                contract = self.registry.contract(service)
            except KeyError:
                contract = None
            if contract is not None:
                valid = [c.id for c in contract.components]
                if want_comp not in valid:
                    problems = [
                        f"tool.name: unknown component {want_comp!r} "
                        f"for service {service!r}; valid components: "
                        + ", ".join(valid)
                    ]
                    return {"error": "query_logs refused",
                            "problems": problems}, "refused", problems
        now = self.clock.now()
        start = now - timedelta(seconds=minutes * 60)
        matched: list[Any] = []
        for entry in self.log_query.entries(start, now):
            routed = self.registry.route(entry)
            deployments = {dep for dep, _comp in routed}
            if deployment not in deployments:
                continue
            if want_comp is not None and (
                deployment, want_comp
            ) not in routed:
                continue
            if filt.get("event") is not None and (
                entry.jsonPayload.get("event") != filt["event"]
            ):
                continue
            if filt.get("minSeverity") is not None and (
                _SEV_RANK.get(entry.severity, -1)
                < _SEV_RANK[filt["minSeverity"]]
            ):
                continue
            if filt.get("runId") is not None and (
                entry.jsonPayload.get("runId") != filt["runId"]
            ):
                continue
            matched.append(entry)
        matched.sort(
            key=lambda e: (e.timestamp, e.insertId), reverse=True
        )
        total = len(matched)
        page = matched[:50]
        entries = [
            {
                "insertId": e.insertId,
                "timestamp": _iso_z(e.timestamp),
                "severity": e.severity,
                "labels": dict(e.labels),
                "jsonPayload": copy.deepcopy(e.jsonPayload),
                "container": e.resource.labels.get("container_name", ""),
            }
            for e in page
        ]
        return {"entries": entries, "total": total,
                "truncated": total > 50}, "accepted", []

    def _tool_get_resource(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        name = args["name"]
        if not _NAME_PATTERNS["any"].fullmatch(name):
            problems = [
                f"tool.name: not a readable resource name: {name!r}"
            ]
            return {"error": "get_resource refused",
                    "problems": problems}, "refused", problems
        try:
            return self.chronicle.get(name), "accepted", []
        except ChronicleError as exc:
            return {"error": exc.body}, "accepted", []

    def _tool_list_connector_logs(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        name = args["name"]
        if not _NAME_PATTERNS["connector"].fullmatch(name):
            problems = [
                f"tool.name: not a connector instance name: {name!r}"
            ]
            return {"error": "list_connector_logs refused",
                    "problems": problems}, "refused", problems
        try:
            response = self.chronicle.list_connector_logs(name, 50)
        except ChronicleError as exc:
            return {"error": exc.body}, "accepted", []
        return {"logs": response.get("logs", []),
                "truncated": "nextPageToken" in response}, "accepted", []

    def _tool_list_job_logs(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        name = args["name"]
        if not _NAME_PATTERNS["job"].fullmatch(name):
            problems = [
                f"tool.name: not a job instance name: {name!r}"
            ]
            return {"error": "list_job_logs refused",
                    "problems": problems}, "refused", problems
        try:
            response = self.chronicle.list_job_logs(name, 50)
        except ChronicleError as exc:
            return {"error": exc.body}, "accepted", []
        return {"logs": response.get("logs", []),
                "truncated": "nextPageToken" in response}, "accepted", []

    def _tool_test_integration_instance(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        name = args["name"]
        if not _NAME_PATTERNS["integration"].fullmatch(name):
            problems = [
                f"tool.name: not an integration instance name: {name!r}"
            ]
            return {"error": "test_integration_instance refused",
                    "problems": problems}, "refused", problems
        try:
            return self.chronicle.execute_test(name), "accepted", []
        except ChronicleError as exc:
            return {"error": exc.body}, "accepted", []

    def _tool_compare_siblings(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        from datetime import timedelta

        service = args["service"]
        component = args["component"]
        minutes = int(args["window"]["lastMinutes"])
        try:
            contract = self.registry.contract(service)
        except KeyError:
            problems = [f"tool.name: unknown service: {service!r}"]
            return {"error": "compare_siblings refused",
                    "problems": problems}, "refused", problems
        valid = [c.id for c in contract.components]
        if component not in valid:
            problems = [
                f"tool.name: unknown component {component!r} "
                f"for service {service!r}; valid components: "
                + ", ".join(valid)
            ]
            return {"error": "compare_siblings refused",
                    "problems": problems}, "refused", problems
        heartbeat: str | None = None
        for tripwire in contract.tripwires:
            if tripwire.type == "cadence" and (
                tripwire.component == component
            ):
                heartbeat = str(tripwire.spec.get("event", ""))
                break
        now = self.clock.now()
        start = now - timedelta(seconds=minutes * 60)
        window_entries = self.log_query.entries(start, now)
        deployments: dict[str, Any] = {}
        for dep in self.registry.deployments(service):
            routed = [
                e for e in window_entries
                if (dep.id, component) in self.registry.route(e)
            ]
            own = [
                e for e in window_entries
                if dep.id in {d for d, _c in self.registry.route(e)}
            ]
            if heartbeat:
                runs = [
                    e for e in routed
                    if e.jsonPayload.get("event") == heartbeat
                ]
            else:
                runs = list(routed)
            groups: dict[str, list[Any]] = {}
            for entry in routed:
                run_id = entry.jsonPayload.get("runId")
                key = run_id if isinstance(run_id, str) else (
                    f"entry:{entry.insertId}"
                )
                groups.setdefault(key, []).append(entry)
            warning_runs = sum(
                1 for members in groups.values()
                if any(
                    _SEV_RANK.get(m.severity, -1) >= _SEV_RANK["WARNING"]
                    for m in members
                )
            )
            error_runs = sum(
                1 for members in groups.values()
                if any(
                    _SEV_RANK.get(m.severity, -1) >= _SEV_RANK["ERROR"]
                    for m in members
                )
            )
            alerts = sum(
                1 for e in own
                if e.jsonPayload.get("event") == "alert.ingested"
            )
            last_hb = max(
                runs, key=lambda e: (e.timestamp, e.insertId),
                default=None,
            )
            worst = max(
                routed,
                key=lambda e: (
                    _SEV_RANK.get(e.severity, -1),
                    e.timestamp, e.insertId,
                ),
                default=None,
            )
            deployments[dep.id] = {
                "runs": len(runs),
                "warningRuns": warning_runs,
                "errorRuns": error_runs,
                "alertsIngested": alerts,
                "lastHeartbeatAt": (
                    _iso_z(last_hb.timestamp) if last_hb else None
                ),
                "lastHeartbeatId": (
                    last_hb.insertId if last_hb else None
                ),
                "worstInsertId": worst.insertId if worst else None,
            }
        return {
            "service": service,
            "component": component,
            "window": {"lastMinutes": minutes},
            "deployments": deployments,
        }, "accepted", []

    def _tool_tripwire_history(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        deployment = args["deployment"]
        return {"deployment": deployment,
                "events": self.state.trip_history(deployment)}, \
            "accepted", []

    def _tool_get_contract(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        service = args["service"]
        try:
            contract = self.registry.contract(service)
        except KeyError:
            problems = [f"tool.name: unknown service: {service!r}"]
            return {"error": "get_contract refused",
                    "problems": problems}, "refused", problems
        data = contract.model_dump(mode="json")
        for tripwire in data.get("tripwires", []):
            tripwire.pop("description", None)
        scorecard = self.registry.scorecard(service)
        summary: dict[str, Any] | None = None
        if scorecard is not None:
            if isinstance(scorecard, dict):
                results = scorecard.get("results", [])
                summary = {
                    "passed": bool(scorecard.get("passed", False)),
                    "expected_hit_rate": float(
                        scorecard.get("expected_hit_rate", 0.0)
                    ),
                    "false_positives": int(
                        scorecard.get("false_positives", 0)
                    ),
                    "scenarios": len(results),
                }
            else:
                summary = {
                    "passed": bool(getattr(scorecard, "passed", False)),
                    "expected_hit_rate": float(
                        getattr(scorecard, "expected_hit_rate", 0.0)
                    ),
                    "false_positives": int(
                        getattr(scorecard, "false_positives", 0)
                    ),
                    "scenarios": len(getattr(scorecard, "results", [])),
                }
        return {"service": service, "contract": data,
                "scorecard": summary}, "accepted", []

    # -- working tools -------------------------------------------------

    def _refused(
        self, name: str, problems: list[str]
    ) -> tuple[dict[str, Any], str, list[str]]:
        return {"error": f"{name} refused",
                "problems": problems}, "refused", problems

    def _tool_set_hypotheses(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        items = args["items"]
        problems = check_hypotheses(items)
        if problems and self.verification:
            return self._refused("set_hypotheses", problems)
        session.hypotheses = {
            item["id"]: Hypothesis(
                id=item["id"],
                statement=item["statement"],
                wouldConfirm=item["wouldConfirm"],
                alternative=bool(item["alternative"]),
            )
            for item in items
        }
        keep = set(session.hypotheses)
        for eid in [
            eid for eid, record in session.evidence.items()
            if record.hypothesis not in keep
        ]:
            del session.evidence[eid]
        result: dict[str, Any] = {"ok": True, "count": len(items)}
        if problems:
            return result, "wouldRefuse", problems
        return result, "accepted", []

    def _tool_update_plan(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        session.plan = list(args["plan"])
        return {"ok": True}, "accepted", []

    def _next_evidence_id(self, session: Any) -> str:
        best = 0
        for eid in session.evidence:
            if eid.startswith("E") and eid[1:].isdigit():
                best = max(best, int(eid[1:]))
        return f"E{best + 1}"

    def _tool_record_evidence(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        incident = self._incident_for(session)
        problems = check_evidence(
            args, session, self.log_query, incident, self.registry
        )
        if problems and self.verification:
            return self._refused("record_evidence", problems)
        source = args["source"]
        eid = self._next_evidence_id(session)
        if "insertId" in source:
            entry = self.log_query.get(source["insertId"])
            routed = (
                {dep for dep, _c in self.registry.route(entry)}
                if entry is not None else set()
            )
            preferred = str(getattr(incident, "deployment", ""))
            if preferred in routed:
                deployment: str | None = preferred
            else:
                deployment = sorted(routed)[0] if routed else None
            record = Evidence(
                id=eid,
                hypothesis=args["hypothesis"],
                stance=args["stance"],
                kind="log",
                insertId=source["insertId"],
                value=entry.model_dump(mode="json")
                if entry is not None else None,
                why=args["why"],
                recordedAtTurn=session.turn,
                deployment=deployment,
            )
        else:
            record = Evidence(
                id=eid,
                hypothesis=args["hypothesis"],
                stance=args["stance"],
                kind="toolResult",
                callId=source["callId"],
                field=source["field"],
                value=source.get("value"),
                why=args["why"],
                recordedAtTurn=session.turn,
                deployment=None,
            )
        session.evidence[eid] = record
        remaining = [
            hid for hid in session.hypotheses
            if all(
                item.hypothesis != hid
                for item in session.evidence.values()
            )
        ]
        result: dict[str, Any] = {
            "ok": True, "id": eid, "remaining": remaining,
        }
        if problems:
            return result, "wouldRefuse", problems
        return result, "accepted", []

    def _tool_submit_diagnosis(
        self, args: dict[str, Any], session: Any
    ) -> tuple[dict[str, Any], str, list[str]]:
        incident = self._incident_for(session)
        try:
            contract = self.registry.contract(incident.service)
        except KeyError:
            return self._refused("submit_diagnosis", [
                "diagnosis.incident: unknown service for incident "
                f"{incident.id!r}"
            ])
        problems = check_diagnosis(
            args["diagnosis"], session, incident, contract,
            self.log_query, self.registry,
        )
        schema_problems = [
            p for p in problems if p.startswith("diagnosis.schema:")
        ]
        if schema_problems:
            return self._refused("submit_diagnosis", problems)
        if problems and self.verification:
            return self._refused("submit_diagnosis", problems)
        action = args["diagnosis"].get("action", {})
        result: dict[str, Any] = {
            "ok": True, "status": "accepted",
            "action": action.get("type", ""),
        }
        if problems:
            return result, "wouldRefuse", problems
        return result, "accepted", []
