"""D5 review round 2: stalled, canary-component and schema category gates.

Stall rules compare cited log evidence to the deployment's latest
heartbeat entries by insertId (never by timestamp), using the cadence
tripwire on the contract's connectorInstance/jobInstance component.
schema_drift requires a cited entry that routes to the incident's
deployment and violates a schema tripwire.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fleetsre.clock import SimClock
from fleetsre.contracts import load_contract
from fleetsre.evaluator.engine import TripEvent
from fleetsre.logmodel import LogEntry, MonitoredResource
from fleetsre.mock.fleet import load_fleet
from fleetsre.ports import MemoryLogQuery, MemorySink, MemoryStateStore
from fleetsre.registry import Registry, deployments_from_fleet
from fleetsre.sre.schemas import Incident
from fleetsre.sre.session import Session
from fleetsre.sre.tools import ToolSet

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(minutes=30)


def _entry(
    insert_id: str,
    ts: datetime,
    env: str,
    event: str,
    *,
    instance: str | None = None,
    severity: str = "INFO",
    run_id: str = "run-1",
    extra_payload: dict[str, Any] | None = None,
) -> LogEntry:
    if instance is None:
        labels = {"correlation_id": f"corr-{insert_id}"}
        container = "etl"
    else:
        labels = {
            "integration_name": "MockSentinel",
            "integration_version": "1.0",
            "connector_name": "Mock Sentinel Incidents Connector",
            "connector_instance": instance,
        }
        container = "python"
    payload: dict[str, Any] = {
        "event": event, "runId": run_id, "environment": env,
    }
    payload.update(extra_payload or {})
    return LogEntry(
        insertId=insert_id,
        timestamp=ts,
        severity=severity,
        logName="projects/test/logs/test",
        resource=MonitoredResource(
            type="k8s_container", labels={"container_name": container}
        ),
        labels=labels,
        jsonPayload=payload,
    )


def _harness(entries: list[LogEntry]) -> tuple[ToolSet, Registry, SimClock]:
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contracts = [
        load_contract(ROOT / "contracts" / "alert-ingestion.json"),
        load_contract(ROOT / "contracts" / "intel-sync.json"),
    ]
    deployments: list[Any] = []
    for contract in contracts:
        deployments.extend(deployments_from_fleet(fleet, contract))
    registry = Registry(contracts, deployments)
    clock = SimClock(NOW)
    sink = MemorySink()
    for entry in entries:
        sink.write(entry)
    tools = ToolSet(
        MemoryLogQuery(sink), None, MemoryStateStore(), registry,
        clock=clock, verification=True,
    )
    return tools, registry, clock


def _incident(registry: Registry, deployment: str, tripwire: str) -> Incident:
    event = TripEvent(
        kind="trip", deployment=deployment, tripwire=tripwire,
        at=NOW, reason="probe", evidence=(),
    )
    return Incident.from_trip(event, registry, NOW)


def _session_with_hypotheses(tools: ToolSet, incident: Incident) -> Session:
    session = Session(incident_id=incident.id)
    out = tools.call("set_hypotheses", {
        "items": [
            {"id": "H1", "statement": "s", "wouldConfirm": "c",
             "alternative": False},
            {"id": "H2", "statement": "s", "wouldConfirm": "c",
             "alternative": True},
        ],
        "rationale": "test",
    }, session)
    assert "error" not in out["result"], out
    return session


def _diagnosis(
    incident: Incident, category: str, evidence: list[str],
    component: str = "connector",
) -> dict[str, Any]:
    return {
        "incidentId": incident.id,
        "deployment": incident.deployment,
        "component": component,
        "category": category,
        "hypothesis": "H1",
        "summary": "s",
        "evidence": evidence,
        "openQuestions": [{"hypothesis": "H2", "why": "open"}],
        "action": {"type": "none"},
        "expectedVerification": {
            "tripwire": incident.tripwire, "condition": "recovers_within",
            "seconds": 300,
        },
        "confidence": "medium",
    }


def _submit(tools: ToolSet, session: Session, diag: dict[str, Any]) -> dict:
    return tools.call(
        "submit_diagnosis", {"diagnosis": diag, "rationale": "t"}, session
    )


def _record(
    tools: ToolSet, session: Session, insert_id: str, hyp: str = "H1",
) -> dict:
    return tools.call("record_evidence", {
        "hypothesis": hyp, "stance": "supports",
        "source": {"insertId": insert_id},
        "why": "w", "rationale": "t",
    }, session)


def test_sibling_same_timestamp_heartbeat_refused():
    """A sibling heartbeat sharing the latest timestamp is not the
    deployment's latest heartbeat: insertIds must match."""
    tools, registry, _clock = _harness([
        _entry("hb-own", NOW, "alpha", "connector.run.completed",
               instance="alpha-sentinel"),
        _entry("hb-sib", NOW, "bravo", "connector.run.completed",
               instance="bravo-sentinel"),
    ])
    incident = _incident(registry, "alert-ingestion/alpha", "dead-connector")
    session = _session_with_hypotheses(tools, incident)
    out = tools.call("query_logs", {
        "deployment": "alert-ingestion/bravo",
        "window": {"lastMinutes": 60}, "filter": {},
        "rationale": "query sibling",
    }, session)
    assert "error" not in out["result"], out
    rec = _record(tools, session, "hb-sib")
    assert "error" not in rec["result"], rec
    eid = rec["result"]["id"]
    refused = _submit(
        tools, session,
        _diagnosis(incident, "connector_stalled", [eid]),
    )
    problems = refused["result"].get("problems", [])
    assert any(p.startswith("diagnosis.category:") for p in problems), \
        refused


def test_own_latest_heartbeat_accepted():
    tools, registry, _clock = _harness([
        _entry("hb-old", NOW - timedelta(minutes=1), "alpha",
               "connector.run.completed", instance="alpha-sentinel"),
        _entry("hb-own", NOW, "alpha", "connector.run.completed",
               instance="alpha-sentinel"),
    ])
    incident = _incident(registry, "alert-ingestion/alpha", "dead-connector")
    session = _session_with_hypotheses(tools, incident)
    rec = _record(tools, session, "hb-own")
    assert "error" not in rec["result"], rec
    ok = _submit(
        tools, session,
        _diagnosis(incident, "connector_stalled", [rec["result"]["id"]]),
    )
    assert "error" not in ok["result"], ok


def test_canary_incident_diagnosed_connector_stalled():
    """connector_stalled uses the connector component's cadence tripwire,
    whatever the incident's component."""
    tools, registry, _clock = _harness([
        _entry("hb-own", NOW, "alpha", "connector.run.completed",
               instance="alpha-sentinel"),
    ])
    incident = _incident(registry, "alert-ingestion/alpha", "canary")
    assert incident.component == "ingestion"
    session = _session_with_hypotheses(tools, incident)
    rec = _record(tools, session, "hb-own")
    assert "error" not in rec["result"], rec
    ok = _submit(
        tools, session,
        _diagnosis(incident, "connector_stalled", [rec["result"]["id"]]),
    )
    assert "error" not in ok["result"], ok


def test_sibling_violation_refused_for_schema_drift():
    tools, registry, _clock = _harness([
        _entry("viol-sib", NOW, "bravo", "alert.ingested", extra_payload={
            "alert": {"priority": "urgent"},
        }),
    ])
    incident = _incident(
        registry, "alert-ingestion/alpha", "priority-shape"
    )
    session = _session_with_hypotheses(tools, incident)
    out = tools.call("query_logs", {
        "deployment": "alert-ingestion/bravo",
        "window": {"lastMinutes": 60}, "filter": {},
        "rationale": "query sibling",
    }, session)
    assert "error" not in out["result"], out
    rec = _record(tools, session, "viol-sib")
    assert "error" not in rec["result"], rec
    refused = _submit(
        tools, session,
        _diagnosis(
            incident, "schema_drift", [rec["result"]["id"]],
            component="ingestion",
        ),
    )
    problems = refused["result"].get("problems", [])
    assert any(p.startswith("diagnosis.category:") for p in problems), \
        refused


def test_heartbeat_refused_for_schema_drift():
    tools, registry, _clock = _harness([
        _entry("hb-own", NOW, "alpha", "connector.run.completed",
               instance="alpha-sentinel"),
    ])
    incident = _incident(
        registry, "alert-ingestion/alpha", "priority-shape"
    )
    session = _session_with_hypotheses(tools, incident)
    rec = _record(tools, session, "hb-own")
    assert "error" not in rec["result"], rec
    refused = _submit(
        tools, session,
        _diagnosis(
            incident, "schema_drift", [rec["result"]["id"]],
            component="ingestion",
        ),
    )
    problems = refused["result"].get("problems", [])
    assert any(p.startswith("diagnosis.category:") for p in problems), \
        refused
