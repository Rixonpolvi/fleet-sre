"""D5 review: diagnosis.resource is exact membership in resource_names."""

from datetime import datetime, timezone
from pathlib import Path

from fleetsre.contracts import load_contract
from fleetsre.evaluator.engine import TripEvent
from fleetsre.mock.fleet import load_fleet
from fleetsre.ports import MemoryLogQuery, MemorySink
from fleetsre.registry import Registry, deployments_from_fleet
from fleetsre.sre.gates import check_diagnosis
from fleetsre.sre.schemas import Hypothesis, Incident
from fleetsre.sre.session import Session

ROOT = Path(__file__).resolve().parents[2]
AT = datetime(2026, 10, 29, 14, 33, 15, tzinfo=timezone.utc)


def _setup(service: str = "alert-ingestion", env: str = "alpha"):
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / f"{service}.json")
    deployments = deployments_from_fleet(fleet, contract)
    registry = Registry([contract], deployments)
    tripwire = next(t.id for t in contract.tripwires if t.type == "cadence")
    event = TripEvent(
        kind="trip", deployment=f"{service}/{env}",
        tripwire=tripwire, at=AT, reason="r", evidence=(),
    )
    incident = Incident.from_trip(event, registry, AT)
    session = Session(incident_id=incident.id)
    session.hypotheses["H1"] = Hypothesis(
        id="H1", statement="s", wouldConfirm="c", alternative=True,
    )
    log_query = MemoryLogQuery(MemorySink())
    return incident, contract, session, log_query, registry


def _diagnosis(
    incident: Incident,
    resource: str | None,
    method: str | None = "connectorInstances.restart",
) -> dict:
    component = next(
        (key for key in incident.resources
         if key != "integrationInstance"),
        "connector",
    )
    return {
        "incidentId": incident.id,
        "deployment": incident.deployment,
        "component": component,
        "category": "unknown",
        "hypothesis": "H1",
        "summary": "s",
        "evidence": [],
        "openQuestions": [],
        "action": {
            "type": "propose", "method": method,
            "resource": resource, "updateMask": [], "patch": {},
        },
        "expectedVerification": {
            "tripwire": incident.tripwire, "condition": "recovers_within",
            "seconds": 180,
        },
        "confidence": "medium",
    }


def test_own_resource_has_no_resource_problem():
    incident, contract, session, log_query, registry = _setup()
    problems = check_diagnosis(
        _diagnosis(incident, incident.resources["connector"]),
        session, incident, contract, log_query, registry,
    )
    assert [p for p in problems if p.startswith("diagnosis.resource:")] == []


def test_foreign_resource_refused():
    incident, contract, session, log_query, registry = _setup()
    foreign = incident.resources["connector"].replace("alpha", "bravo")
    assert foreign != incident.resources["connector"]
    problems = check_diagnosis(
        _diagnosis(incident, foreign),
        session, incident, contract, log_query, registry,
    )
    assert any(p.startswith("diagnosis.resource:") for p in problems)


def test_prefix_mangled_resource_refused():
    """Same trailing ID but not the deployment's full name: refused."""
    incident, contract, session, log_query, registry = _setup()
    mangled = "bogus/connectorInstances/alpha-sentinel"
    problems = check_diagnosis(
        _diagnosis(incident, mangled),
        session, incident, contract, log_query, registry,
    )
    assert any(p.startswith("diagnosis.resource:") for p in problems)


def test_incident_resources_come_from_fleet_names():
    incident, _contract, _s, _q, registry = _setup()
    dep = registry.deployment("alert-ingestion/alpha")
    assert incident.resources == dict(dep.resource_names)
    assert set(incident.resources) == {"connector", "integrationInstance"}


def test_integration_instance_name_refused_as_resource():
    incident, contract, session, log_query, registry = _setup()
    problems = check_diagnosis(
        _diagnosis(incident, incident.resources["integrationInstance"]),
        session, incident, contract, log_query, registry,
    )
    assert any(p.startswith("diagnosis.resource:") for p in problems), \
        problems


def test_job_name_with_connector_method_refused():
    incident, _c, session, log_query, registry = _setup(
        service="intel-sync", env="alpha"
    )
    contract = registry.contract("intel-sync")
    problems = check_diagnosis(
        _diagnosis(
            incident, incident.resources["job"],
            method="connectorInstances.restart",
        ),
        session, incident, contract, log_query, registry,
    )
    assert any(p.startswith("diagnosis.resource:") for p in problems), \
        problems


def test_job_name_with_job_method_has_no_resource_problem():
    incident, _c, session, log_query, registry = _setup(
        service="intel-sync", env="alpha"
    )
    contract = registry.contract("intel-sync")
    problems = check_diagnosis(
        _diagnosis(
            incident, incident.resources["job"],
            method="jobInstances.restart",
        ),
        session, incident, contract, log_query, registry,
    )
    assert [
        p for p in problems if p.startswith("diagnosis.resource:")
    ] == []
