"""D2 SRE feasibility spike.

Interface (docs/deliverables.md §D2):
  python spikes/sre_spike.py --fixture <dir> [--model {scripted|<model id>}]
      [--runs N] [--gates on|off] [--script <file>]

--model defaults to $FLEETSRE_SPIKE_MODEL, else muse-spark-1.3.
--script defaults to <fixture>/scripted.json.
Stdout per run: one TRACE line per turn, then RESULT, then DIAGNOSIS if accepted.
RESULT is {"run","status","turns","readCalls","refusals","multiCallTurns"}:
"status" is "accepted" iff the run ends with an accepted submission, and
"multiCallTurns" counts turns whose model response held more than one
function_call (only the first call runs).
Exit 0 when the program completes, whatever the run outcomes.

Stub tools read the fixture dir only. incident.json "now" is the current
time: no tool returns anything after it.
Read-tool result contracts:
  query_logs -> {"entries": [...], "total": int, "truncated": bool}
    At most 50 entries, newest first. "total" counts every match at or
    before incident now; entries after now never match. "truncated" is
    true when matches exceed 50. "deployment" matches
    jsonPayload.environment; "window" is a half-open "<start>/<end>" interval
    of ISO-8601 UTC instants; "filter" is "" (all), "severity >= WARNING",
    or "event = <jsonPayload.event>".
  get_resource -> the resource object exactly as in resources.json
  list_connector_logs -> {"logs": [...], "total": int, "truncated": bool}
    At most 50 records, newest first, mirroring the fixture order.
    Records with "time" (unix ms) after incident now are excluded;
    "total" counts the records at or before now.
  test_integration_instance -> {"success": bool, "message": str}
  compare_siblings -> {"window": str, "deployments": {deployment: signals}}
    "window" echoes the request. Per deployment of the service, computed
    from logs.jsonl over the window: {"runs": int, "warningRuns": int,
    "errorRuns": int, "alertsIngested": int, "lastRunAt": str}. "runs"
    counts connector.run.completed entries; warning/errorRuns count runs
    with a WARNING/ERROR entry; alertsIngested counts alert.ingested;
    lastRunAt is the newest completed timestamp. No verdict, only signals.

Minimal gates (inside the working tools): hypotheses before evidence or
submission; 2-4 hypotheses with at least one alternative; a log source's
insertId exists; a tool source's callId is a call made in this run; cited
evidence IDs were recorded. A refused record_evidence call consumes no
evidence ID. A tool source's "field" is a dotted path into that call's
result (e.g. "deployments.alert-ingestion/alpha.warningRuns"); the gate
refuses when the path is missing or its value differs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

READ_TOOLS = {
    "query_logs",
    "get_resource",
    "list_connector_logs",
    "test_integration_instance",
    "compare_siblings",
}

MAX_TURNS = 24
MAX_READ_CALLS = 12

NO_TOOL_MSG = "Plain text is not accepted. Call exactly one tool."
LAST_TURN_MSG = (
    "This is the last turn (turn 24 of 24). "
    "Submit your diagnosis with what you have."
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _resolve_field(result: object, field: str) -> tuple[bool, object]:
    cur: object = result
    for part in field.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _values_equal(actual: object, expected: object) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def _format_tool_output(call_id: str, result: dict) -> str:
    return json.dumps({"callId": call_id, "result": result})


class StubTools:
    """Stub tools over one fixture dir."""

    def __init__(self, fixture_dir: str | Path) -> None:
        """Bind to a fixture dir holding logs.jsonl, resources.json, incident.json."""
        self.fixture_dir = Path(fixture_dir)
        logs_path = self.fixture_dir / "logs.jsonl"
        entries: list[dict] = []
        for line in logs_path.read_text().splitlines():
            if line.strip():
                entries.append(json.loads(line))
        self._logs = entries
        self._ids = {e.get("insertId") for e in entries}
        self._resources = json.loads(
            (self.fixture_dir / "resources.json").read_text()
        )
        self._incident = json.loads(
            (self.fixture_dir / "incident.json").read_text()
        )
        self._now = _parse_ts(self._incident["now"])
        self._now_ms = int(self._now.timestamp() * 1000)

    def query_logs(
        self, deployment: str, window: str, filter: str, rationale: str
    ) -> dict:
        """Return {"entries", "total", "truncated"} from logs.jsonl (cap 50)."""
        _ = rationale
        filters_help = (
            'accepted filters: "" (all), "severity >= WARNING", '
            '"event = <jsonPayload.event name>"'
        )
        window_help = "window must be ISO start/ISO end, e.g. 2026-10-29T15:00:00Z/2026-10-29T16:00:00Z"
        env = deployment.split("/", 1)[1] if "/" in deployment else deployment
        try:
            start_s, end_s = window.split("/", 1)
            start = _parse_ts(start_s)
            end = _parse_ts(end_s)
        except (ValueError, AttributeError):
            return {
                "error": f"bad window {window!r}; {window_help}",
                "problems": [window_help],
            }
        if filter != "" and filter != "severity >= WARNING" and not filter.startswith("event = "):
            return {
                "error": f"unsupported filter {filter!r}; {filters_help}",
                "problems": [filters_help],
            }
        want_event = filter[len("event = "):] if filter.startswith("event = ") else None
        matched: list[dict] = []
        for e in self._logs:
            payload = e.get("jsonPayload", {})
            if payload.get("environment") != env:
                continue
            ts = _parse_ts(e["timestamp"])
            if not (start <= ts < end):
                continue
            if ts > self._now:
                continue
            if filter == "severity >= WARNING":
                if e.get("severity") not in ("WARNING", "ERROR"):
                    continue
            elif want_event is not None:
                if payload.get("event") != want_event:
                    continue
            matched.append(e)
        matched.sort(key=lambda e: e["timestamp"], reverse=True)
        total = len(matched)
        return {
            "entries": matched[:50],
            "total": total,
            "truncated": total > 50,
        }

    def get_resource(self, name: str, rationale: str) -> dict:
        """Return the named resource exactly as in resources.json."""
        _ = rationale
        for conn in self._resources.get("connectorInstances", []):
            if conn.get("name") == name:
                return conn
        for integ in self._resources.get("integrationInstances", []):
            if integ.get("name") == name:
                return integ
        return {"error": f"resource not found: {name}"}

    def list_connector_logs(
        self, connectorInstance: str, rationale: str
    ) -> dict:
        """Return {"logs", "total", "truncated"} for the instance (cap 50)."""
        _ = rationale
        all_logs = self._resources.get("connectorLogs", {})
        if connectorInstance not in all_logs:
            return {"error": f"unknown connector instance: {connectorInstance}"}
        filtered = [
            r for r in all_logs[connectorInstance]
            if int(r["time"]) <= self._now_ms
        ]
        total = len(filtered)
        return {"logs": filtered[:50], "total": total, "truncated": total > 50}

    def test_integration_instance(self, name: str, rationale: str) -> dict:
        """Return {"success", "message"} for the integration instance."""
        _ = rationale
        for integ in self._resources.get("integrationInstances", []):
            if integ.get("name") == name:
                ident = integ.get("identifier")
                tests = self._resources.get("executeTest", {})
                if ident in tests:
                    return tests[ident]
                return {"error": f"no executeTest for: {ident}"}
        return {"error": f"integration instance not found: {name}"}

    def compare_siblings(
        self, service: str, component: str, window: str, rationale: str
    ) -> dict:
        """Return {"window", "deployments"} signals from logs.jsonl."""
        _ = rationale
        _ = component
        window_help = "window must be ISO start/ISO end, e.g. 2026-10-29T15:00:00Z/2026-10-29T16:00:00Z"
        try:
            start_s, end_s = window.split("/", 1)
            start = _parse_ts(start_s)
            end = _parse_ts(end_s)
        except (ValueError, AttributeError):
            return {
                "error": f"bad window {window!r}; {window_help}",
                "problems": [window_help],
            }
        envs = sorted({
            e.get("jsonPayload", {}).get("environment")
            for e in self._logs
            if e.get("jsonPayload", {}).get("environment")
        })
        out: dict[str, dict] = {}
        for env in envs:
            in_window = []
            for e in self._logs:
                if e.get("jsonPayload", {}).get("environment") != env:
                    continue
                ts = _parse_ts(e["timestamp"])
                if not (start <= ts < end):
                    continue
                if ts > self._now:
                    continue
                in_window.append(e)
            completed = [
                e for e in in_window
                if e.get("jsonPayload", {}).get("event") == "connector.run.completed"
            ]
            warn_runs = {
                e["jsonPayload"]["runId"]
                for e in in_window
                if e.get("severity") == "WARNING" and "runId" in e.get("jsonPayload", {})
            }
            error_runs = {
                e["jsonPayload"]["runId"]
                for e in in_window
                if e.get("severity") == "ERROR" and "runId" in e.get("jsonPayload", {})
            }
            alerts = sum(
                1 for e in in_window
                if e.get("jsonPayload", {}).get("event") == "alert.ingested"
            )
            last = max((e["timestamp"] for e in completed), default=None)
            out[f"{service}/{env}"] = {
                "runs": len(completed),
                "warningRuns": len(warn_runs),
                "errorRuns": len(error_runs),
                "alertsIngested": alerts,
                "lastRunAt": last,
            }
        return {"window": window, "deployments": out}


class _RunState:
    def __init__(self) -> None:
        self.hypotheses: dict[str, dict] | None = None
        self.plan: object = None
        self.evidence: dict[str, dict] = {}
        self.evidence_counter = 0
        self.calls: dict[str, dict] = {}
        self.read_calls = 0
        self.refusals_by_gate: dict[str, int] = {}
        self.multi_call_turns = 0
        self.accepted_diagnosis: dict | None = None


def _tool_specs() -> list[dict]:
    s = {"type": "string"}
    rat = {"type": "string", "description": "Visible think line, max 200 chars."}
    diag_schema: dict = {"type": "object"}
    loaded = _load_schema()
    if isinstance(loaded, dict):
        diag_schema = {
            k: v for k, v in loaded.items()
            if k not in ("$schema", "title", "description")
        }
    return [
        {
            "type": "function", "name": "query_logs",
            "description": (
                "Query log entries for a deployment over a window. "
                "window is ISO start/ISO end, e.g. "
                "2026-10-29T15:00:00Z/2026-10-29T16:00:00Z. "
                'filter is one of: "" (all), "severity >= WARNING", '
                '"event = <jsonPayload.event name>".'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "deployment": s, "window": s, "filter": s, "rationale": rat,
                },
                "required": ["deployment", "window", "filter", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function", "name": "get_resource",
            "description": "Get a connector or integration instance by full name.",
            "parameters": {
                "type": "object",
                "properties": {"name": s, "rationale": rat},
                "required": ["name", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function", "name": "list_connector_logs",
            "description": "List recent ConnectorLog records for an instance.",
            "parameters": {
                "type": "object",
                "properties": {"connectorInstance": s, "rationale": rat},
                "required": ["connectorInstance", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function", "name": "test_integration_instance",
            "description": "Run a connectivity test (no state change).",
            "parameters": {
                "type": "object",
                "properties": {"name": s, "rationale": rat},
                "required": ["name", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function", "name": "compare_siblings",
            "description": "Compare the same component across deployments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": s, "component": s, "window": s, "rationale": rat,
                },
                "required": ["service", "component", "window", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function", "name": "set_hypotheses",
            "description": "Set 2-4 hypotheses with at least one alternative.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": s, "statement": s,
                                "wouldConfirm": s, "alternative": {"type": "boolean"},
                            },
                            "required": ["id", "statement", "wouldConfirm", "alternative"],
                            "additionalProperties": False,
                        },
                    },
                    "rationale": rat,
                },
                "required": ["items", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function", "name": "update_plan",
            "description": "Record how each hypothesis will be settled.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {"type": "array", "items": {"type": "object"}},
                    "rationale": rat,
                },
                "required": ["plan", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function", "name": "record_evidence",
            "description": (
                "Record one supports/refutes item for a hypothesis. "
                'source is {"insertId": str} for a log entry or '
                '{"callId": str, "field": dotted path, "value": ...} '
                "for a tool result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hypothesis": s,
                    "stance": {"type": "string", "enum": ["supports", "refutes"]},
                    "source": {"type": "object"},
                    "why": s, "rationale": rat,
                },
                "required": ["hypothesis", "stance", "source", "why", "rationale"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function", "name": "submit_diagnosis",
            "description": "Submit the diagnosis for the incident.",
            "parameters": {
                "type": "object",
                "properties": {
                    "diagnosis": diag_schema,
                    "rationale": rat,
                },
                "required": ["diagnosis", "rationale"],
                "additionalProperties": False,
            },
        },
    ]


def _load_schema() -> dict | None:
    p = Path(__file__).parent / "diagnosis_min.schema.json"
    try:
        return json.loads(p.read_text())
    except OSError:
        return None


def _validate_diagnosis(diagnosis: object, schema: dict | None) -> list[str]:
    problems: list[str] = []
    if schema is None:
        return problems
    try:
        import jsonschema

        v = jsonschema.Draft202012Validator(schema)
        for err in sorted(v.iter_errors(diagnosis), key=lambda e: list(e.path)):
            loc = ".".join(str(x) for x in err.path) or "(root)"
            problems.append(f"schema: {loc}: {err.message}")
    except ImportError:
        if not isinstance(diagnosis, dict):
            return ["schema: diagnosis must be an object"]
        for key in ("deployment", "category", "hypothesis", "evidence", "action"):
            if not isinstance(diagnosis, dict) or key not in diagnosis:
                problems.append(f"schema: missing required field: {key}")
        cats = {
            "connector_stalled", "job_stalled", "credential_failure",
            "schema_drift", "job_hollow_success", "job_failing", "unknown",
        }
        if isinstance(diagnosis, dict) and diagnosis.get("category") not in cats:
            problems.append("schema: invalid category")
        ev = diagnosis.get("evidence") if isinstance(diagnosis, dict) else None
        if not isinstance(ev, list) or not ev:
            problems.append("schema: evidence must be a non-empty array")
        act = diagnosis.get("action") if isinstance(diagnosis, dict) else None
        if not isinstance(act, dict) or act.get("type") not in ("propose", "none", "escalate"):
            problems.append("schema: action.type must be propose, none or escalate")
    return problems


def _execute(
    tools: StubTools,
    state: _RunState,
    name: str,
    args: dict,
    call_id: str,
    gates_on: bool,
    schema: dict | None,
) -> tuple[str, dict]:
    if name in READ_TOOLS:
        if state.read_calls >= MAX_READ_CALLS:
            state.refusals_by_gate["read_budget"] = (
                state.refusals_by_gate.get("read_budget", 0) + 1
            )
            result = {
                "error": "read budget exceeded: 12 read calls per session",
                "problems": ["read budget exceeded"],
            }
            state.calls[call_id] = {"tool": name, "args": args, "result": result}
            return "refused", result
        state.read_calls += 1
        try:
            if name == "query_logs":
                result = tools.query_logs(
                    deployment=args.get("deployment", ""),
                    window=args.get("window", ""),
                    filter=args.get("filter", ""),
                    rationale=args.get("rationale", ""),
                )
            elif name == "get_resource":
                result = tools.get_resource(
                    name=args.get("name", ""),
                    rationale=args.get("rationale", ""),
                )
            elif name == "list_connector_logs":
                result = tools.list_connector_logs(
                    connectorInstance=args.get("connectorInstance", ""),
                    rationale=args.get("rationale", ""),
                )
            elif name == "test_integration_instance":
                result = tools.test_integration_instance(
                    name=args.get("name", ""),
                    rationale=args.get("rationale", ""),
                )
            else:
                result = tools.compare_siblings(
                    service=args.get("service", ""),
                    component=args.get("component", ""),
                    window=args.get("window", ""),
                    rationale=args.get("rationale", ""),
                )
        except Exception as exc:
            result = {"error": str(exc), "problems": [str(exc)]}
        if not isinstance(result, dict):
            result = {"value": result}
        state.calls[call_id] = {"tool": name, "args": args, "result": result}
        if "error" in result:
            state.refusals_by_gate[name] = state.refusals_by_gate.get(name, 0) + 1
            return "refused", result
        return "accepted", result

    if name == "set_hypotheses":
        problems: list[str] = []
        items = args.get("items")
        if not isinstance(items, list):
            problems.append("items must be a list")
            items_list: list = []
        else:
            items_list = items
            if not (2 <= len(items_list) <= 4):
                problems.append(f"need 2-4 hypotheses, got {len(items_list)}")
            if not any(
                isinstance(it, dict) and it.get("alternative") is True
                for it in items_list
            ):
                problems.append("at least one hypothesis must set alternative: true")
            seen: set[str] = set()
            for it in items_list:
                if not isinstance(it, dict) or not it.get("id"):
                    problems.append("every hypothesis needs an id")
                    break
                if it["id"] in seen:
                    problems.append(f"duplicate hypothesis id: {it['id']}")
                    break
                seen.add(it["id"])
        if problems:
            state.refusals_by_gate["set_hypotheses"] = (
                state.refusals_by_gate.get("set_hypotheses", 0) + 1
            )
            result_err = {"error": "set_hypotheses refused", "problems": problems}
            state.calls[call_id] = {"tool": name, "args": args, "result": result_err}
            if not gates_on:
                if isinstance(items, list) and items:
                    new_h = {
                        it["id"]: it for it in items if isinstance(it, dict) and it.get("id")
                    }
                    if new_h:
                        state.hypotheses = new_h
                        drop = [k for k, v in state.evidence.items() if v.get("hypothesis") not in new_h]
                        for k in drop:
                            del state.evidence[k]
                    ok = {"ok": True, "count": len(items)}
                    state.calls[call_id] = {"tool": name, "args": args, "result": ok}
                    return "would_refuse", ok
                return "would_refuse", result_err
            return "refused", result_err
        new_h = {it["id"]: it for it in items_list}
        state.hypotheses = new_h
        drop = [k for k, v in state.evidence.items() if v.get("hypothesis") not in new_h]
        for k in drop:
            del state.evidence[k]
        result_ok = {"ok": True, "count": len(items_list)}
        state.calls[call_id] = {"tool": name, "args": args, "result": result_ok}
        return "accepted", result_ok

    if name == "update_plan":
        state.plan = args.get("plan")
        result_ok = {"ok": True}
        state.calls[call_id] = {"tool": name, "args": args, "result": result_ok}
        return "accepted", result_ok

    if name == "record_evidence":
        problems = []
        if state.hypotheses is None:
            problems.append("set_hypotheses must be accepted before record_evidence")
        hyp = args.get("hypothesis")
        if state.hypotheses is not None and hyp not in state.hypotheses:
            problems.append(f"unknown hypothesis: {hyp}")
        if args.get("stance") not in ("supports", "refutes"):
            problems.append("stance must be supports or refutes")
        source = args.get("source")
        if not isinstance(source, dict):
            problems.append("source must be an object")
        elif "insertId" in source:
            if source.get("insertId") not in tools._ids:
                problems.append(f"insertId not found: {source.get('insertId')}")
        elif "callId" in source:
            cid = source.get("callId")
            if cid not in state.calls:
                problems.append(f"callId not in this run: {cid}")
            else:
                field = source.get("field")
                if not isinstance(field, str) or not field:
                    problems.append("tool source needs a field path")
                else:
                    found, actual = _resolve_field(state.calls[cid]["result"], field)
                    if not found:
                        problems.append(f"field not found: {field}")
                    elif not _values_equal(actual, source.get("value")):
                        problems.append(
                            f"field value differs: {field} is {actual!r}"
                        )
        else:
            problems.append("source must have insertId or callId")
        if problems:
            state.refusals_by_gate["record_evidence"] = (
                state.refusals_by_gate.get("record_evidence", 0) + 1
            )
            result_err = {"error": "record_evidence refused", "problems": problems}
            state.calls[call_id] = {"tool": name, "args": args, "result": result_err}
            if not gates_on:
                state.evidence_counter += 1
                eid = f"E{state.evidence_counter}"
                state.evidence[eid] = {
                    "hypothesis": hyp, "stance": args.get("stance"),
                    "source": source, "why": args.get("why"),
                }
                ok = {"ok": True, "id": eid}
                state.calls[call_id] = {"tool": name, "args": args, "result": ok}
                return "would_refuse", ok
            return "refused", result_err
        state.evidence_counter += 1
        eid = f"E{state.evidence_counter}"
        state.evidence[eid] = {
            "hypothesis": hyp, "stance": args.get("stance"),
            "source": source, "why": args.get("why"),
        }
        remaining = [
            hid for hid in (state.hypotheses or {})
            if all(v.get("hypothesis") != hid for v in state.evidence.values())
        ]
        result_ok = {"ok": True, "id": eid, "remaining": remaining}
        state.calls[call_id] = {"tool": name, "args": args, "result": result_ok}
        return "accepted", result_ok

    if name == "submit_diagnosis":
        problems = []
        if state.hypotheses is None:
            problems.append("set_hypotheses must be accepted before submit_diagnosis")
        diagnosis = args.get("diagnosis")
        if not isinstance(diagnosis, dict):
            problems.append("diagnosis must be an object")
            cited: list = []
        else:
            problems.extend(_validate_diagnosis(diagnosis, schema))
            cited = diagnosis.get("evidence", [])
            if isinstance(cited, list):
                for eid in cited:
                    if eid not in state.evidence:
                        problems.append(f"evidence not recorded: {eid}")
            if state.hypotheses is not None and diagnosis.get("hypothesis") not in state.hypotheses:
                problems.append(f"unknown hypothesis: {diagnosis.get('hypothesis')}")
        if problems:
            state.refusals_by_gate["submit_diagnosis"] = (
                state.refusals_by_gate.get("submit_diagnosis", 0) + 1
            )
            result_err = {"error": "submit_diagnosis refused", "problems": problems}
            state.calls[call_id] = {"tool": name, "args": args, "result": result_err}
            if not gates_on:
                if isinstance(diagnosis, dict):
                    state.accepted_diagnosis = diagnosis
                ok = {"ok": True, "status": "accepted"}
                state.calls[call_id] = {"tool": name, "args": args, "result": ok}
                return "would_refuse", ok
            return "refused", result_err
        state.accepted_diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
        result_ok = {"ok": True, "status": "accepted"}
        state.calls[call_id] = {"tool": name, "args": args, "result": result_ok}
        return "accepted", result_ok

    result_err = {"error": f"unknown tool: {name}", "problems": [f"unknown tool: {name}"]}
    state.calls[call_id] = {"tool": name, "args": args, "result": result_err}
    state.refusals_by_gate[name] = state.refusals_by_gate.get(name, 0) + 1
    return "refused", result_err


def _parse_output(output: list[dict]) -> tuple[list[dict], list[dict]]:
    calls = [it for it in output if isinstance(it, dict) and it.get("type") == "function_call"]
    return output, calls


def _run_scripted(
    fixture: Path,
    script_path: Path,
    gates_on: bool,
    run_id: int,
) -> tuple[dict, dict | None]:
    tools = StubTools(fixture)
    state = _RunState()
    schema = _load_schema()
    script = json.loads(script_path.read_text())
    status = "exhausted"
    turns_done = 0
    for idx, output in enumerate(script):
        turn = idx + 1
        if turn > MAX_TURNS:
            break
        turns_done = turn
        _, calls = _parse_output(output)
        if len(calls) > 1:
            state.multi_call_turns += 1
        if not calls:
            print("TRACE " + json.dumps({
                "run": run_id, "turn": turn, "tool": "",
                "verdict": "no_tool_call", "rationale": "",
            }), flush=True)
            continue
        first = calls[0]
        call_id = str(first.get("call_id", f"call_{turn:03d}"))
        tname = str(first.get("name", ""))
        try:
            args = json.loads(first.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        rationale = args.get("rationale", "")
        if not isinstance(rationale, str):
            rationale = str(rationale)
        verdict, result = _execute(tools, state, tname, args, call_id, gates_on, schema)
        trace: dict = {
            "run": run_id, "turn": turn, "tool": tname,
            "verdict": verdict, "rationale": rationale,
        }
        if verdict in ("refused", "would_refuse"):
            trace["problems"] = result.get("problems", [])
        print("TRACE " + json.dumps(trace), flush=True)
        if tname == "submit_diagnosis" and verdict in ("accepted", "would_refuse"):
            if gates_on and verdict == "accepted":
                status = "accepted"
                break
            if not gates_on:
                status = "accepted"
                break
    else:
        if state.accepted_diagnosis is not None:
            status = "accepted"
    if status != "accepted" and turns_done >= MAX_TURNS:
        status = "budget"
    refusals = sum(state.refusals_by_gate.values())
    result = {
        "run": run_id, "status": status, "turns": turns_done,
        "readCalls": state.read_calls, "refusals": refusals,
        "multiCallTurns": state.multi_call_turns,
    }
    print("RESULT " + json.dumps(result), flush=True)
    diag = None
    if status == "accepted" and state.accepted_diagnosis is not None:
        diag = state.accepted_diagnosis
        print("DIAGNOSIS " + json.dumps(diag), flush=True)
    return result, diag


def _build_incident_message(
    fixture: Path, tools: StubTools, incident: dict
) -> str:
    deployment = str(incident.get("deployment", ""))
    service, _, env = deployment.partition("/")
    resources = tools._resources
    connector_name = "unknown"
    connector_id = "unknown"
    for c in resources.get("connectorInstances", []):
        if str(c.get("environment", "")).split("/")[-1] == env:
            connector_name = str(c.get("name", connector_name))
            connector_id = str(
                c.get("identifier", c.get("id", connector_id))
            )
            break
    integration_name = "unknown"
    for it in resources.get("integrationInstances", []):
        if str(it.get("environment", "")).split("/")[-1] == env:
            integration_name = str(it.get("name", integration_name))
            break
    envs = sorted({
        str(c.get("environment", "")).split("/")[-1]
        for c in resources.get("connectorInstances", [])
        if c.get("environment")
    })
    siblings = [f"{service}/{e}" for e in envs if e and e != env]
    trip_def: dict | None = None
    try:
        contract_path = (
            Path(__file__).parent.parent / "contracts" / f"{service}.json"
        )
        contract = json.loads(contract_path.read_text())
        for tw in contract.get("tripwires", []):
            if tw.get("id") == incident.get("tripwire"):
                trip_def = tw
                break
    except OSError:
        trip_def = None
    lines = [
        "Incident:",
        json.dumps(incident, indent=2),
        "",
        f"Service: {service}",
        f"Connector instance: {connector_name} (ID: {connector_id}; "
        "pass the ID to list_connector_logs)",
        f"Integration instance: {integration_name}",
        f"Sibling deployments: {', '.join(siblings) if siblings else 'none'}",
        f"Tripwire definition (contracts/{service}.json):",
        json.dumps(trip_def, indent=2) if trip_def is not None else "unknown",
    ]
    _ = fixture
    return "\n".join(lines)


def _load_prompt() -> str:
    p = Path(__file__).parent / "prompt.md"
    try:
        text = p.read_text().strip()
        if text:
            return text
    except OSError:
        pass
    return (
        "You are the Fleet SRE investigator. Call exactly one tool per turn. "
        "Set hypotheses first (2-4, at least one alternative), record evidence "
        "before citing it, then submit a diagnosis."
    )


def _live_call_with_backoff(client: object, kwargs: dict) -> object:
    last_exc: Exception | None = None
    delay = 1.0
    for attempt in range(6):
        try:
            return client.responses.create(**kwargs)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - SDK error types vary
            name = type(exc).__name__
            msg = str(exc)
            status = getattr(exc, "status_code", None)
            if status is None:
                status = getattr(getattr(exc, "response", None), "status_code", None)
            is_429 = status == 429 or "429" in name or "429" in msg or "RateLimit" in name
            last_exc = exc
            if not is_429 or attempt >= 5:
                raise
            time.sleep(delay)
            delay *= 2.0
    assert last_exc is not None
    raise last_exc


def _usage_numbers(usage: object) -> tuple[int, int, int | None, int | None]:
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    cached: int | None = None
    reasoning: int | None = None
    det_in = getattr(usage, "input_tokens_details", None)
    if det_in is not None:
        cached = getattr(det_in, "cached_tokens", None)
        if cached is not None:
            cached = int(cached)
    det_out = getattr(usage, "output_tokens_details", None)
    if det_out is not None:
        reasoning = getattr(det_out, "reasoning_tokens", None)
        if reasoning is not None:
            reasoning = int(reasoning)
    return inp, out, cached, reasoning


def _append_results(
    model: str, run_id: int, turns: int, state: _RunState,
    total_in: int, total_out: int,
    total_cached: int | None, cached_seen: bool,
    total_reasoning: int | None, reasoning_seen: bool,
    latency_s: float, status: str, error: str | None = None,
) -> None:
    try:
        price_in = float(os.environ.get("FLEETSRE_PRICE_IN", "0") or 0)
    except ValueError:
        price_in = 0.0
    try:
        price_out = float(os.environ.get("FLEETSRE_PRICE_OUT", "0") or 0)
    except ValueError:
        price_out = 0.0
    cost = (total_in * price_in + total_out * price_out) / 1_000_000.0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cached_s = str(total_cached) if cached_seen else "null"
    reasoning_s = str(total_reasoning) if reasoning_seen else "null"
    entry = (
        f"\n## {now} model {model} run {run_id}\n"
        f"- date: {now}\n"
        f"- model: {model}\n"
        f"- tokens in: {total_in} (cached: {cached_s})\n"
        f"- tokens out: {total_out} (reasoning: {reasoning_s})\n"
        f"- turns: {turns}\n"
        f"- read calls: {state.read_calls}\n"
        f"- refusals by gate: {json.dumps(state.refusals_by_gate)}\n"
        f"- multiCallTurns: {state.multi_call_turns}\n"
        f"- latency: {latency_s:.1f}s\n"
        f"- final status: {status}\n"
        + (f"- error: {error}\n" if error else "")
        + f"- estimated cost: ${cost:.4f} "
        f"(rates FLEETSRE_PRICE_IN={price_in}/M, FLEETSRE_PRICE_OUT={price_out}/M; "
        f"preview pricing is unconfirmed)\n"
    )
    path = Path(__file__).parent / "RESULTS.md"
    with path.open("a") as fh:
        fh.write(entry)


def _run_live(
    fixture: Path,
    model: str,
    gates_on: bool,
    run_id: int,
) -> tuple[dict, dict | None]:
    if "MODEL_API_KEY" not in os.environ or not os.environ["MODEL_API_KEY"]:
        print("MODEL_API_KEY is not set", file=sys.stderr)
        raise SystemExit(2)
    try:
        from openai import OpenAI
    except ImportError:
        print("openai package is required for live runs", file=sys.stderr)
        raise SystemExit(2)
    api_key = os.environ["MODEL_API_KEY"]
    client = OpenAI(api_key=api_key, base_url="https://api.meta.ai/v1")
    tools = StubTools(fixture)
    state = _RunState()
    schema = _load_schema()
    system_prompt = _load_prompt()
    incident = json.loads((fixture / "incident.json").read_text())
    history: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _build_incident_message(fixture, tools, incident)},
    ]
    specs = _tool_specs()
    total_in = 0
    total_out = 0
    total_cached = 0
    total_reasoning = 0
    cached_seen = False
    reasoning_seen = False
    status = "budget"
    model_error: str | None = None
    start = time.time()
    turns_done = 0
    try:
        for turn in range(1, MAX_TURNS + 1):
            turns_done = turn
            request_input = list(history)
            if turn == MAX_TURNS:
                request_input.append({"role": "user", "content": LAST_TURN_MSG})
                history.append({"role": "user", "content": LAST_TURN_MSG})
            try:
                resp = _live_call_with_backoff(client, {
                    "model": model,
                    "input": request_input,
                    "tools": specs,
                    "store": False,
                    "include": ["reasoning.encrypted_content"],
                    "parallel_tool_calls": False,
                    "reasoning": {"effort": "medium"},
                })
            except Exception as exc:
                status = "model_error"
                model_error = str(exc)
                print("TRACE " + json.dumps({
                    "run": run_id, "turn": turn, "tool": "",
                    "verdict": "no_tool_call", "rationale": "",
                }), flush=True)
                print(f"model call failed: {exc}", file=sys.stderr)
                break
            usage = getattr(resp, "usage", None)
            if usage is not None:
                try:
                    i, o, c, r = _usage_numbers(usage)
                except Exception:
                    i, o, c, r = 0, 0, None, None
                total_in += i
                total_out += o
                if c is not None:
                    total_cached += c
                    cached_seen = True
                if r is not None:
                    total_reasoning += r
                    reasoning_seen = True
            raw_output = list(getattr(resp, "output", []) or [])
            output: list[dict] = []
            for item in raw_output:
                if isinstance(item, dict):
                    output.append(item)
                elif hasattr(item, "model_dump"):
                    try:
                        output.append(item.model_dump(exclude_none=True))  # type: ignore[no-any-return]
                    except Exception:
                        output.append({"type": getattr(item, "type", "unknown")})
                else:
                    try:
                        output.append(dict(item))
                    except Exception:
                        output.append({"type": getattr(item, "type", "unknown")})
            _, calls = _parse_output(output)
            if len(calls) > 1:
                state.multi_call_turns += 1
            history.extend(output)
            if not calls:
                print("TRACE " + json.dumps({
                    "run": run_id, "turn": turn, "tool": "",
                    "verdict": "no_tool_call", "rationale": "",
                }), flush=True)
                history.append({"role": "user", "content": NO_TOOL_MSG})
                continue
            first = calls[0]
            call_id = str(first.get("call_id", f"call_{turn:03d}"))
            tname = str(first.get("name", ""))
            try:
                args = json.loads(first.get("arguments", "{}") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            rationale = args.get("rationale", "")
            if not isinstance(rationale, str):
                rationale = str(rationale)
            verdict, result = _execute(tools, state, tname, args, call_id, gates_on, schema)
            trace = {
                "run": run_id, "turn": turn, "tool": tname,
                "verdict": verdict, "rationale": rationale,
            }
            if verdict in ("refused", "would_refuse"):
                trace["problems"] = result.get("problems", [])
            print("TRACE " + json.dumps(trace), flush=True)
            history.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": _format_tool_output(call_id, result),
            })
            for extra in calls[1:]:
                extra_id = str(extra.get("call_id", ""))
                if extra_id:
                    history.append({
                        "type": "function_call_output",
                        "call_id": extra_id,
                        "output": _format_tool_output(extra_id, {
                            "error": "not executed: only the first call in a response runs"
                        }),
                    })
            if tname == "submit_diagnosis" and verdict in ("accepted", "would_refuse"):
                status = "accepted"
                break
    finally:
        latency = time.time() - start
    refusals = sum(state.refusals_by_gate.values())
    result: dict = {
        "run": run_id, "status": status, "turns": turns_done,
        "readCalls": state.read_calls, "refusals": refusals,
        "multiCallTurns": state.multi_call_turns,
    }
    if status == "model_error" and model_error:
        result["error"] = model_error
    print("RESULT " + json.dumps(result), flush=True)
    diag = None
    if status == "accepted" and state.accepted_diagnosis is not None:
        diag = state.accepted_diagnosis
        print("DIAGNOSIS " + json.dumps(diag), flush=True)
    _append_results(
        model, run_id, turns_done, state, total_in, total_out,
        total_cached, cached_seen, total_reasoning, reasoning_seen,
        latency, status, model_error,
    )
    return result, diag


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="D2 SRE feasibility spike")
    parser.add_argument("--fixture", required=True)
    parser.add_argument(
        "--model",
        default=os.environ.get("FLEETSRE_SPIKE_MODEL", "muse-spark-1.3"),
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--gates", choices=["on", "off"], default="on")
    parser.add_argument("--script", default=None)
    args = parser.parse_args(argv)
    fixture = Path(args.fixture)
    gates_on = args.gates == "on"
    script_path = Path(args.script) if args.script else fixture / "scripted.json"
    for run_id in range(1, args.runs + 1):
        if args.model == "scripted":
            _run_scripted(fixture, script_path, gates_on, run_id)
        else:
            _run_live(fixture, args.model, gates_on, run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
