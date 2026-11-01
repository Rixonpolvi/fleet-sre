"""D5 review round 2: submit_diagnosis schema-first validation.

The tool spec carries required lists and the condition enum; argument
errors under the diagnosis path are reported as diagnosis.schema.
Schema problems refuse even with verification=False and no hypotheses.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fleetsre.clock import SimClock
from fleetsre.contracts import load_contract
from fleetsre.evaluator.engine import TripEvent
from fleetsre.mock.fleet import load_fleet
from fleetsre.ports import MemoryLogQuery, MemorySink, MemoryStateStore
from fleetsre.registry import Registry, deployments_from_fleet
from fleetsre.sre.schemas import Incident
from fleetsre.sre.session import Session
from fleetsre.sre.tools import ToolSet

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 29, 14, 33, 15, tzinfo=timezone.utc)


def _tools(verification: bool = True) -> tuple[ToolSet, Incident]:
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / "alert-ingestion.json")
    deployments = deployments_from_fleet(fleet, contract)
    registry = Registry([contract], deployments)
    event = TripEvent(
        kind="trip", deployment="alert-ingestion/alpha",
        tripwire="dead-connector", at=NOW, reason="r", evidence=(),
    )
    incident = Incident.from_trip(event, registry, NOW)
    tools = ToolSet(
        MemoryLogQuery(MemorySink()), None, MemoryStateStore(), registry,
        clock=SimClock(NOW), verification=verification,
    )
    return tools, incident


def _full_diagnosis(incident: Incident) -> dict[str, Any]:
    return {
        "incidentId": incident.id,
        "deployment": incident.deployment,
        "component": "connector",
        "category": "connector_stalled",
        "hypothesis": "H1",
        "summary": "s",
        "evidence": ["E99"],
        "openQuestions": [{"hypothesis": "H2", "why": "open"}],
        "action": {"type": "none"},
        "expectedVerification": {
            "tripwire": "dead-connector", "condition": "recovers_within",
            "seconds": 300,
        },
        "confidence": "medium",
    }


def _set_ok(tools: ToolSet, session: Session) -> None:
    out = tools.call("set_hypotheses", {
        "items": [
            {"id": "H1", "statement": "s", "wouldConfirm": "c",
             "alternative": False},
            {"id": "H2", "statement": "s", "wouldConfirm": "c",
             "alternative": True},
        ],
        "rationale": "t",
    }, session)
    assert "error" not in out["result"], out


def test_gates_off_no_hypotheses_schema_invalid_refused():
    tools, incident = _tools(verification=False)
    session = Session(incident_id=incident.id)
    out = tools.call("submit_diagnosis", {
        "diagnosis": {"incidentId": incident.id}, "rationale": "t",
    }, session)
    assert "error" in out["result"], out
    assert session.calls[out["callId"]].verdict == "refused"
    assert any(
        p.startswith("diagnosis.schema:")
        for p in out["result"].get("problems", [])
    ), out


def test_missing_required_reported_as_diagnosis_schema():
    tools, incident = _tools()
    session = Session(incident_id=incident.id)
    _set_ok(tools, session)
    out = tools.call("submit_diagnosis", {
        "diagnosis": {"incidentId": incident.id}, "rationale": "t",
    }, session)
    problems = out["result"].get("problems", [])
    assert any(p.startswith("diagnosis.schema:") for p in problems), out
    assert not any(p.startswith("args.schema:") for p in problems), out


def test_bad_condition_enum_reported_as_diagnosis_schema():
    tools, incident = _tools()
    session = Session(incident_id=incident.id)
    _set_ok(tools, session)
    diag = _full_diagnosis(incident)
    diag["expectedVerification"]["condition"] = "recovers_eventually"
    out = tools.call(
        "submit_diagnosis", {"diagnosis": diag, "rationale": "t"}, session
    )
    problems = out["result"].get("problems", [])
    assert any(p.startswith("diagnosis.schema:") for p in problems), out


def test_schema_problem_carries_json_path():
    tools, incident = _tools()
    session = Session(incident_id=incident.id)
    _set_ok(tools, session)
    diag = _full_diagnosis(incident)
    del diag["action"]["type"]
    out = tools.call(
        "submit_diagnosis", {"diagnosis": diag, "rationale": "t"}, session
    )
    assert out["result"].get("problems", []) == [
        "diagnosis.schema: action: 'type' is a required property"
    ], out


def test_misnested_expected_verification_hint():
    tools, incident = _tools()
    session = Session(incident_id=incident.id)
    _set_ok(tools, session)
    diag = _full_diagnosis(incident)
    diag["action"]["expectedVerification"] = diag.pop(
        "expectedVerification"
    )
    out = tools.call(
        "submit_diagnosis", {"diagnosis": diag, "rationale": "t"}, session
    )
    assert out["result"].get("problems", []) == [
        "diagnosis.schema: action: additional property "
        "'expectedVerification' (it belongs beside action)"
    ], out


def test_gate_side_misnested_hint():
    from fleetsre.sre.gates import check_diagnosis

    tools, incident = _tools()
    session = Session(incident_id=incident.id)
    _set_ok(tools, session)
    diag = _full_diagnosis(incident)
    diag["action"]["expectedVerification"] = {
        "tripwire": "t", "condition": "c", "seconds": 1,
    }
    problems = check_diagnosis(
        diag, session, incident, tools.registry.contract(
            "alert-ingestion"
        ), tools.log_query, tools.registry,
    )
    assert (
        "diagnosis.schema: action: additional property "
        "'expectedVerification' (it belongs beside action)"
    ) in problems, problems


def test_args_schema_names_path():
    tools, incident = _tools()
    session = Session(incident_id=incident.id)
    out = tools.call("query_logs", {
        "deployment": "alert-ingestion/alpha",
        "window": {"lastMinutes": 0}, "filter": {},
        "rationale": "t",
    }, session)
    assert out["result"].get("problems", []) == [
        "args.schema: window.lastMinutes: "
        "0 is less than the minimum of 1"
    ], out


def test_component_problem_lists_valid_ids():
    tools, incident = _tools()
    session = Session(incident_id=incident.id)
    _set_ok(tools, session)
    diag = _full_diagnosis(incident)
    diag["component"] = "nope"
    out = tools.call(
        "submit_diagnosis", {"diagnosis": diag, "rationale": "t"}, session
    )
    matches = [
        p for p in out["result"].get("problems", [])
        if p.startswith("diagnosis.component:")
    ]
    assert len(matches) == 1, out
    assert "connector" in matches[0] and "ingestion" in matches[0]


def test_spec_valid_diagnosis_reaches_gate_rules():
    tools, incident = _tools()
    session = Session(incident_id=incident.id)
    _set_ok(tools, session)
    out = tools.call("submit_diagnosis", {
        "diagnosis": _full_diagnosis(incident), "rationale": "t",
    }, session)
    problems = out["result"].get("problems", [])
    assert any(p.startswith("diagnosis.evidence:") for p in problems), out
    assert not any(p.startswith("diagnosis.schema:") for p in problems), out
