"""Gates: pure checks run inside the working tools (spec.md §6.3, §6.5)."""

import re
from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from fleetsre.contracts import SchemaSpec
from fleetsre.evaluator.tripwires import schema_violation
from fleetsre.sre.schemas import Diagnosis

_HYP_ID_RE = re.compile(r"^H[1-9][0-9]*$")

_SEV_RANK = {
    "DEBUG": 0,
    "INFO": 1,
    "NOTICE": 2,
    "WARNING": 3,
    "ERROR": 4,
    "CRITICAL": 5,
    "ALERT": 6,
    "EMERGENCY": 7,
}

_ALLOWLIST = {
    "connectorInstances.patch": {"enabled", "intervalSeconds", "parameters"},
    "connectorInstances.restart": set(),
    "connectorInstances.runOnDemand": set(),
    "jobInstances.patch": {"enabled", "parameters"},
    "jobInstances.restart": set(),
    "jobInstances.runOnDemand": set(),
}


def check_hypotheses(items: list[dict[str, Any]]) -> list[str]:
    """Check a set_hypotheses item list (d5-pins.md pin 26)."""
    problems: list[str] = []
    if not isinstance(items, list):
        return ["hypotheses.count: items must be a list"]
    if not 2 <= len(items) <= 4:
        problems.append(
            f"hypotheses.count: need 2-4 hypotheses, got {len(items)}"
        )
    if not any(
        isinstance(item, dict) and item.get("alternative") is True
        for item in items
    ):
        problems.append(
            "hypotheses.alternative: at least one hypothesis must be "
            "marked alternative"
        )
    seen: set[str] = set()
    for item in items:
        hid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(hid, str) or not _HYP_ID_RE.match(hid):
            problems.append(f"hypotheses.id: bad hypothesis id: {hid!r}")
            continue
        if hid in seen:
            problems.append(f"hypotheses.id: duplicate hypothesis id: {hid}")
        seen.add(hid)
    return problems


def _resolve_field(result: Any, field: str) -> tuple[bool, Any]:
    if field.startswith("result."):
        field = field[len("result."):]
    node: Any = result
    for part in field.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit():
            idx = int(part)
            if idx >= len(node):
                return False, None
            node = node[idx]
        else:
            return False, None
    return True, node


def _json_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return bool(actual == expected)
    if type(actual) is not type(expected):
        return False
    return bool(actual == expected)


def _queried_deployments(session: Any, registry: Any) -> set[str]:
    """Deployments citable as evidence: queried via query_logs or siblings."""
    queried: set[str] = set()
    calls = getattr(session, "calls", {})
    for record in calls.values():
        tool = getattr(record, "tool", "")
        if getattr(record, "verdict", "") != "accepted":
            continue
        args = getattr(record, "args", {})
        if tool == "query_logs" and isinstance(args.get("deployment"), str):
            queried.add(args["deployment"])
        elif tool == "compare_siblings" and isinstance(
            args.get("service"), str
        ):
            for dep in registry.deployments(args["service"]):
                queried.add(dep.id)
    return queried


def check_evidence(
    args: dict[str, Any],
    session: Any,
    log_query: Any,
    incident: Any,
    registry: Any,
) -> list[str]:
    """Check a record_evidence call (d5-pins.md pin 27)."""
    problems: list[str] = []
    hypotheses = getattr(session, "hypotheses", {})
    if not hypotheses:
        return [
            "order: set_hypotheses must be accepted before record_evidence"
        ]
    hyp = args.get("hypothesis")
    if hyp not in hypotheses:
        problems.append(f"evidence.hypothesis: unknown hypothesis: {hyp!r}")
    same = sum(
        1 for e in getattr(session, "evidence", {}).values()
        if getattr(e, "hypothesis", None) == hyp
    )
    if same >= 3:
        problems.append(
            f"evidence.limit: hypothesis {hyp} already has 3 items"
        )
    source = args.get("source")
    if not isinstance(source, dict):
        problems.append("evidence.insert_id: source must be an object")
        return problems
    if "insertId" in source:
        insert_id = source.get("insertId")
        entry = log_query.get(insert_id)
        if entry is None:
            problems.append(
                f"evidence.insert_id: insertId not found: {insert_id!r}"
            )
            return problems
        now = getattr(incident, "opened_at", None)
        if now is not None and entry.timestamp > now:
            problems.append(
                f"evidence.future: insertId {insert_id!r} is after now"
            )
        routed = {dep for dep, _comp in registry.route(entry)}
        allowed = _queried_deployments(session, registry)
        allowed.add(str(getattr(incident, "deployment", "")))
        if not (routed & allowed):
            problems.append(
                f"evidence.deployment: insertId {insert_id!r} routes "
                f"to {sorted(routed) or 'nothing'}, not the incident "
                "deployment or a queried sibling"
            )
    elif "callId" in source:
        from fleetsre.sre.tools import READ_TOOLS

        call_id = source.get("callId")
        calls = getattr(session, "calls", {})
        record = calls.get(call_id)
        if (
            record is None
            or getattr(record, "verdict", "") != "accepted"
            or getattr(record, "tool", "") not in READ_TOOLS
        ):
            problems.append(
                f"evidence.call_id: callId is not an accepted read-tool "
                f"call of this session: {call_id!r}"
            )
            return problems
        field = source.get("field")
        if not isinstance(field, str) or not field:
            problems.append("evidence.field: tool source needs a field path")
            return problems
        found, actual = _resolve_field(
            getattr(record, "result", {}), field
        )
        if not found:
            problems.append(
                f"evidence.field: field not found in {call_id!r}: {field}"
            )
        elif not _json_equal(actual, source.get("value")):
            problems.append(
                f"evidence.value: field {field} is {actual!r}, "
                f"not {source.get('value')!r}"
            )
    else:
        problems.append(
            "evidence.insert_id: source must name insertId or callId"
        )
    return problems


def _trip_at(incident: Any) -> Any:
    event = getattr(incident, "trip_event", None)
    if isinstance(event, dict):
        raw = event.get("at", "")
        if isinstance(raw, str) and raw:
            from datetime import datetime

            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return None
        return raw if raw else None
    return getattr(event, "at", None)


def _resolve_param(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return params.get(value[2:-1], value)
    if isinstance(value, dict):
        return {k: _resolve_param(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_param(v, params) for v in value]
    return value


def _schema_spec(tripwire: Any, params: dict[str, Any]) -> SchemaSpec:
    spec = dict(getattr(tripwire, "spec", {}))
    return SchemaSpec(
        event=spec.get("event"),
        field=str(spec["field"]),
        type=str(spec["type"]),
        enum=spec.get("enum"),
        minimum=_resolve_param(spec.get("minimum"), params),
        required=bool(spec["required"]),
        consecutive=int(_resolve_param(spec.get("consecutive", 1), params)),
        recoverAfter=_resolve_param(spec.get("recoverAfter"), params),
        where=spec.get("where"),
    )


def _expected_resource(
    method: Any, incident: Any, contract: Any
) -> str | None:
    """Full resource name a propose method requires, by exact match.

    connectorInstances.* needs the deployment's connector instance
    name, jobInstances.* its job instance name; anything else (the
    integration instance included) has no valid resource.
    """
    if not isinstance(method, str):
        return None
    if method.startswith("connectorInstances."):
        want_kind = "connectorInstance"
    elif method.startswith("jobInstances."):
        want_kind = "jobInstance"
    else:
        return None
    resources = getattr(incident, "resources", {})
    for component in getattr(contract, "components", []):
        if component.kind == want_kind and component.id in resources:
            return str(resources[component.id])
    return None


def _log_entries(
    cited: list[str], session: Any, log_query: Any
) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    evidence = getattr(session, "evidence", {})
    for eid in cited:
        record = evidence.get(eid)
        if record is None or getattr(record, "kind", "") != "log":
            continue
        entry = log_query.get(getattr(record, "insertId", None))
        if entry is not None:
            out.append((eid, entry))
    return out


def _category_problems(
    diag: Diagnosis,
    session: Any,
    incident: Any,
    contract: Any,
    log_query: Any,
    registry: Any,
) -> list[str]:
    category = diag.category
    deployment = str(getattr(incident, "deployment", ""))
    if category == "unknown":
        action_type = diag.action.type
        if action_type != "escalate":
            return [
                "diagnosis.category: category unknown requires action "
                "escalate"
            ]
        return []
    cited = list(diag.evidence)
    logged = _log_entries(cited, session, log_query)
    if category in ("connector_stalled", "job_stalled"):
        want_kind = (
            "connectorInstance" if category == "connector_stalled"
            else "jobInstance"
        )
        kinds = {
            c.id: c.kind for c in getattr(contract, "components", [])
        }
        heartbeat: str | None = None
        for tripwire in getattr(contract, "tripwires", []):
            if (
                tripwire.type == "cadence"
                and kinds.get(tripwire.component) == want_kind
            ):
                heartbeat = str(tripwire.spec.get("event", ""))
                break
        if not heartbeat:
            return [
                f"diagnosis.category: no cadence heartbeat for a "
                f"{want_kind} component"
            ]
        trip_at = _trip_at(incident)
        if trip_at is None:
            return ["diagnosis.category: incident has no trip time"]
        candidates = [
            e for e in log_query.entries(
                trip_at - timedelta(hours=24), trip_at
            )
            if e.jsonPayload.get("event") == heartbeat
            and any(
                dep == deployment for dep, _c in registry.route(e)
            )
        ]
        if not candidates:
            return [
                "diagnosis.category: no heartbeat found for the deployment"
            ]
        latest_ts = max(e.timestamp for e in candidates)
        latest_ids = {
            e.insertId for e in candidates if e.timestamp == latest_ts
        }
        cited_ids = {e.insertId for _eid, e in logged}
        if not (cited_ids & latest_ids):
            return [
                "diagnosis.category: cited evidence lacks the deployment's "
                f"latest {heartbeat} at or before the trip"
            ]
        return []
    if category == "credential_failure":
        connector_comps = {
            c.id for c in getattr(contract, "components", [])
            if c.kind == "connectorInstance"
        }
        warned = any(
            _SEV_RANK.get(e.severity, -1) >= _SEV_RANK["WARNING"]
            and any(
                dep == deployment and comp in connector_comps
                for dep, comp in registry.route(e)
            )
            for _eid, e in logged
        )
        own_integration = getattr(incident, "resources", {}).get(
            "integrationInstance", ""
        )
        tested = False
        evidence = getattr(session, "evidence", {})
        calls = getattr(session, "calls", {})
        for eid in cited:
            record = evidence.get(eid)
            if record is None or getattr(record, "kind", "") != "toolResult":
                continue
            call = calls.get(getattr(record, "callId", None))
            if call is None:
                continue
            if getattr(call, "tool", "") != "test_integration_instance":
                continue
            if getattr(call, "args", {}).get("name") != own_integration:
                continue
            if getattr(call, "result", {}).get("successful") is False:
                tested = True
        problems = []
        if not warned:
            problems.append(
                "diagnosis.category: cited evidence lacks a WARNING-or-"
                "worse entry from the deployment's connector"
            )
        if not tested:
            problems.append(
                "diagnosis.category: cited evidence lacks a "
                "test_integration_instance result with successful: false "
                "for the deployment's own integration instance"
            )
        return problems
    if category == "schema_drift":
        try:
            dep = registry.deployment(deployment)
            params = dict(dep.parameters)
        except KeyError:
            params = {}
        schemas = [
            _schema_spec(t, params)
            for t in getattr(contract, "tripwires", [])
            if t.type == "schema"
        ]
        for _eid, entry in logged:
            # schema_violation already applies the tripwire's event and
            # where filters (it returns None for non-matching entries);
            # the gate adds the deployment route check.
            if not any(
                dep == deployment for dep, _c in registry.route(entry)
            ):
                continue
            for spec in schemas:
                if schema_violation(entry, spec) is not None:
                    return []
        return [
            "diagnosis.category: cited evidence lacks an entry of the "
            "incident's deployment violating a schema tripwire"
        ]
    if category == "job_hollow_success":
        for _eid, entry in logged:
            payload = entry.jsonPayload
            if (
                payload.get("event") == "job.run.completed"
                and payload.get("status") == "SUCCESS"
                and payload.get("indicatorsSynced") == 0
                and any(
                    dep == deployment for dep, _c in registry.route(entry)
                )
            ):
                return []
        return [
            "diagnosis.category: cited evidence lacks a job.run.completed "
            "entry with status SUCCESS and indicatorsSynced 0"
        ]
    if category == "job_failing":
        for _eid, entry in logged:
            payload = entry.jsonPayload
            if not any(
                dep == deployment for dep, _c in registry.route(entry)
            ):
                continue
            if payload.get("event") == "job.run.error":
                return []
            if (
                payload.get("event") == "job.run.completed"
                and payload.get("status") == "ERROR"
            ):
                return []
        return [
            "diagnosis.category: cited evidence lacks a job.run.error "
            "entry or a job.run.completed entry with status ERROR"
        ]
    return [f"diagnosis.category: unknown category {category}"]


def check_diagnosis(
    d: dict[str, Any],
    session: Any,
    incident: Any,
    contract: Any,
    log_query: Any,
    registry: Any,
) -> list[str]:
    """Check a submit_diagnosis payload (spec.md §6.5, d5-pins.md pin 28-29).

    The schema validates first: schema problems return before any other
    rule, so diagnosis.schema refuses even with verification=False and
    no hypotheses (pin 30).
    """
    problems: list[str] = []
    if not isinstance(d, dict):
        return ["diagnosis.schema: diagnosis must be an object"]
    try:
        diag = Diagnosis.model_validate(d)
    except ValidationError as exc:
        for err in exc.errors():
            if err["type"] == "extra_forbidden" and tuple(
                err["loc"]
            ) == ("action", "expectedVerification"):
                problems.append(
                    "diagnosis.schema: action: additional property "
                    "'expectedVerification' (it belongs beside action)"
                )
                continue
            loc = ".".join(str(x) for x in err["loc"]) or "(root)"
            problems.append(f"diagnosis.schema: {loc}: {err['msg']}")
        return problems
    if not getattr(session, "hypotheses", {}):
        return [
            "order: set_hypotheses must be accepted before submit_diagnosis"
        ]
    evidence = getattr(session, "evidence", {})
    for eid in diag.evidence:
        if eid not in evidence:
            problems.append(
                f"diagnosis.evidence: evidence not recorded: {eid}"
            )
    if diag.incidentId != getattr(incident, "id", None):
        problems.append(
            f"diagnosis.incident: incidentId {diag.incidentId!r} is not "
            f"the incident's"
        )
    if diag.deployment != getattr(incident, "deployment", None):
        problems.append(
            f"diagnosis.incident: deployment {diag.deployment!r} is not "
            "the incident's"
        )
    components = list(getattr(contract, "components", []))
    if diag.component not in {c.id for c in components}:
        valid = ", ".join(c.id for c in components)
        problems.append(
            f"diagnosis.component: unknown component "
            f"{diag.component!r}; valid components: {valid}"
        )
    hypotheses = getattr(session, "hypotheses", {})
    if diag.hypothesis not in hypotheses:
        problems.append(
            f"diagnosis.hypothesis: unknown hypothesis: {diag.hypothesis!r}"
        )
    else:
        supports = {
            getattr(record, "hypothesis", None)
            for eid in diag.evidence
            if (record := evidence.get(eid)) is not None
            and getattr(record, "stance", "") == "supports"
        }
        if diag.hypothesis not in supports:
            problems.append(
                "diagnosis.coverage: the concluded hypothesis has no "
                "cited supports item"
            )
        refuted = {
            getattr(record, "hypothesis", None)
            for record in evidence.values()
            if getattr(record, "stance", "") == "refutes"
        }
        asked = {q.hypothesis for q in diag.openQuestions}
        for hid in hypotheses:
            if hid == diag.hypothesis:
                continue
            if hid not in refuted and hid not in asked:
                problems.append(
                    "diagnosis.coverage: hypothesis "
                    f"{hid} has no recorded refutes item or openQuestions "
                    "entry"
                )
    problems.extend(
        _category_problems(
            diag, session, incident, contract, log_query, registry
        )
    )
    action = diag.action
    if action.type == "propose":
        if action.method not in _ALLOWLIST:
            problems.append(
                f"diagnosis.action: method outside the allowlist: "
                f"{action.method!r}"
            )
        else:
            allowed = _ALLOWLIST[action.method]
            mask = action.updateMask or []
            for field in mask:
                if field not in allowed:
                    problems.append(
                        f"diagnosis.update_mask: updateMask field outside "
                        f"{action.method}'s fields: {field!r}"
                    )
            patch = action.patch or {}
            for key in patch:
                if key not in mask:
                    problems.append(
                        f"diagnosis.update_mask: patch key outside "
                        f"updateMask: {key!r}"
                    )
        expected = _expected_resource(action.method, incident, contract)
        if action.resource != expected:
            problems.append(
                f"diagnosis.resource: resource is not the deployment's "
                f"{action.method} name: {action.resource!r}"
            )
    elif action.type == "escalate":
        summary = action.escalationSummary or ""
        if not summary.strip():
            problems.append(
                "diagnosis.action: escalate needs a non-empty "
                "escalationSummary"
            )
    tripwire_ids = {t.id for t in getattr(contract, "tripwires", [])}
    if action.type in ("propose", "none"):
        if diag.expectedVerification is None:
            problems.append(
                "diagnosis.verification: expectedVerification is required"
            )
        elif diag.expectedVerification.tripwire not in tripwire_ids:
            problems.append(
                "diagnosis.verification: tripwire is not this "
                f"deployment's: {diag.expectedVerification.tripwire!r}"
            )
    elif diag.expectedVerification is not None and (
        diag.expectedVerification.tripwire not in tripwire_ids
    ):
        problems.append(
            "diagnosis.verification: tripwire is not this "
            f"deployment's: {diag.expectedVerification.tripwire!r}"
        )
    if action.type == "none":
        resumed = list(getattr(session, "resumed_at_turns", []))
        if resumed:
            bar = max(resumed)
            if not any(
                getattr(record, "recordedAtTurn", 0) > bar
                for eid in diag.evidence
                if (record := evidence.get(eid)) is not None
            ):
                problems.append(
                    "diagnosis.none_after_resume: no cited item was "
                    "recorded after the last resume"
                )
    return problems
