"""D6a unit collateral: control paths the acceptance suite never drives.

Acceptance covers approval/concurrency/journal/crash E2E; these pin the
executor's runOnDemand and invalid-patch paths, restart-already-applied
recovery, the verifier's transitions, and the Investigator/reopen wiring.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fleetsre.adapters.chronicle_http import ChronicleHttpClient
from fleetsre.clock import SimClock
from fleetsre.contracts import load_contract
from fleetsre.control.approvals import ApprovalStore
from fleetsre.control.executor import (
    ApplyError,
    ApplyResult,
    Executor,
    InvalidPatch,
)
from fleetsre.control.incidents import IncidentManager, IncidentState
from fleetsre.control.journal import Journal
from fleetsre.control.proposals import (
    Proposal,
    proposal_hash,
    resource_version,
)
from fleetsre.control.verify import Verifier
from fleetsre.evaluator.engine import Evaluator, TripEvent
from fleetsre.mock.chronicle_api import create_app
from fleetsre.mock.fleet import load_fleet
from fleetsre.mock.world import World
from fleetsre.ports import MemoryLogQuery, MemorySink, MemoryStateStore
from fleetsre.registry import Registry, deployments_from_fleet
from fleetsre.sre.gateway import (
    GatewayConfig,
    ModelGateway,
    RoleConfig,
    ScriptedTransport,
)
from fleetsre.sre.investigator import (
    InvestigationResult,
    Investigator,
    reopen,
)
from fleetsre.sre.schemas import Incident
from fleetsre.sre.session import MemorySessionStore
from fleetsre.sre.tools import ToolSet

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)

CONNECTOR_TMPL = (
    "projects/fleet-sre-demo/locations/us/instances/mock-soar"
    "/integrations/MockSentinel/connectors/"
    "mock-sentinel-incidents/connectorInstances/{}"
)
JOB_TMPL = (
    "projects/fleet-sre-demo/locations/us/instances/mock-soar"
    "/integrations/MockThreatIntel/jobs/sync-indicators/"
    "jobInstances/{}"
)


def _stack(seed: int = 11) -> dict[str, Any]:
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    clock = SimClock(T0)
    world = World(fleet, seed, clock, MemorySink())
    http = TestClient(create_app(world, dev_auth=True))
    deployer = ChronicleHttpClient(http, "deployer")
    approvals = ApprovalStore(clock)
    journal = Journal()
    return {
        "fleet": fleet, "clock": clock, "world": world,
        "deployer": deployer, "approvals": approvals,
        "journal": journal,
        "executor": Executor(deployer, approvals, journal, clock=clock),
    }


def _connector(fleet: Any, env: str) -> str:
    inst = fleet.environments[env].deployments[
        "alert-ingestion"
    ].connectorInstance
    return CONNECTOR_TMPL.format(inst)


def _job(fleet: Any, env: str) -> str:
    inst = fleet.environments[env].deployments["intel-sync"].jobInstance
    return JOB_TMPL.format(inst)


def _proposal(**overrides: Any) -> Proposal:
    base: dict[str, Any] = {
        "incident_id": "inc-test-1",
        "seq": 1,
        "hash": "hash-test-1",
        "method": "connectorInstances.restart",
        "resource": "resource-test-1",
        "update_mask": [],
        "patch": {},
        "resource_version": "v-test-1",
        "diagnosis": {"action": {"type": "propose"}},
        "created_at": T0,
    }
    base.update(overrides)
    return Proposal(**base)


def _incident(service_env: str, tripwire: str) -> Incident:
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(
        ROOT / "contracts" / f"{service_env.split('/', 1)[0]}.json"
    )
    registry = Registry(
        [contract], deployments_from_fleet(fleet, contract)
    )
    event = TripEvent(
        kind="trip", deployment=service_env, tripwire=tripwire,
        at=T0, reason="probe", evidence=(),
    )
    return Incident.from_trip(event, registry, T0)


def test_proposal_hash_counts_seq():
    """Same patch, new seq: a new hash (d6a-pins 3)."""
    first = proposal_hash(
        "inc-1", 1, "connectorInstances.patch", "res", ["enabled"],
        {"enabled": True}, "3",
    )
    second = proposal_hash(
        "inc-1", 2, "connectorInstances.patch", "res", ["enabled"],
        {"enabled": True}, "3",
    )
    assert first != second
    assert len(first) == 64
    assert (
        proposal_hash(
            "inc-1", 1, "connectorInstances.patch", "res",
            ["enabled"], {"enabled": True}, "3",
        )
        == first
    )


def test_resource_version_picks_field_by_kind():
    """Connectors use version, jobs use updateTime (d6a-pins 3)."""
    stack = _stack()
    connector = stack["deployer"].get(_connector(stack["fleet"], "alpha"))
    assert resource_version(connector) == str(connector["version"])
    job = stack["deployer"].get(_job(stack["fleet"], "alpha"))
    assert resource_version(job) == job["updateTime"]
    with pytest.raises(ValueError):
        resource_version({"name": "projects/x/environments/y"})


def test_approvals_number_and_validate():
    """A<n>/R<n> IDs; blank rejection notes raise (d6a-pins 2)."""
    clock = SimClock(T0)
    store = ApprovalStore(clock)
    assert store.approve("inc-1", "h1", "op").id == "A1"
    assert store.approve("inc-1", "h2", "op").id == "A2"
    assert store.reject("inc-1", "h1", "op", "notes").id == "R1"
    with pytest.raises(ValueError):
        store.reject("inc-1", "h1", "op", "   ")
    assert store.approval_for("h2") is not None
    assert store.approval_for("h2").id == "A2"
    assert store.approval_for("missing") is None
    assert [r.id for r in store.records()] == ["A1", "A2", "R1"]


def test_journal_file_round_trip(tmp_path):
    """File journals persist lines and reload them (d6a-pins 4)."""
    path = tmp_path / "journal.jsonl"
    first = Journal(path)
    first.append({"type": "intent", "proposal_hash": "h"})
    assert path.is_file()
    second = Journal(path)
    assert second.records() == [{"type": "intent", "proposal_hash": "h"}]
    assert first.records() == second.records()


def test_unknown_parameter_raises_before_journal():
    """An unknown displayName is InvalidPatch; nothing journaled (pin 18)."""
    stack = _stack()
    resource = _connector(stack["fleet"], "alpha")
    current = stack["deployer"].get(resource)
    incident = _incident("alert-ingestion/alpha", "dead-connector")
    proposal = _proposal(
        incident_id=incident.id, method="connectorInstances.patch",
        resource=resource, update_mask=["parameters"],
        patch={"parameters": [{"displayName": "Nope", "value": "1"}]},
        resource_version=resource_version(current),
    )
    stack["approvals"].approve(incident.id, proposal.hash, "op")
    with pytest.raises(InvalidPatch) as exc:
        stack["executor"].apply(incident, proposal)
    assert "Nope" in str(exc.value.problems)
    assert stack["journal"].records() == []


def test_run_on_demand_bodies():
    """Connector runs carry the resource; job runs carry {} (pin 18)."""
    stack = _stack()
    seen: list[dict[str, Any]] = []
    inner = stack["deployer"].run_on_demand

    def spy(name: str, body: dict[str, Any]) -> dict[str, Any]:
        seen.append({"name": name, "body": body})
        return inner(name, body)

    stack["deployer"].run_on_demand = spy  # type: ignore[method-assign]
    connector = _connector(stack["fleet"], "alpha")
    job = _job(stack["fleet"], "alpha")
    cases = [
        ("alert-ingestion/alpha", "dead-connector", connector,
         "connectorInstances.runOnDemand"),
        ("intel-sync/alpha", "dead-job", job, "jobInstances.runOnDemand"),
    ]
    for deployment, tripwire, resource, method in cases:
        incident = _incident(deployment, tripwire)
        proposal = _proposal(
            incident_id=incident.id, hash=f"hash-{method}",
            method=method, resource=resource,
            resource_version=resource_version(
                stack["deployer"].get(resource)
            ),
        )
        stack["approvals"].approve(incident.id, proposal.hash, "op")
        result = stack["executor"].apply(incident, proposal)
        assert result.outcome == "applied"
        assert result.api_calls == 1
    assert seen[0]["name"] == connector
    assert seen[0]["body"]["connectorInstance"]["name"] == connector
    assert seen[1]["name"] == job
    assert seen[1]["body"] == {}
    assert [r["type"] for r in stack["journal"].records()] == [
        "intent", "started", "step", "terminal",
        "intent", "started", "step", "terminal",
    ]


def test_recover_reruns_run_on_demand():
    """runOnDemand leaves no trace, so recover runs it again (pin 19)."""
    stack = _stack()
    resource = _connector(stack["fleet"], "alpha")
    incident = _incident("alert-ingestion/alpha", "dead-connector")
    proposal = _proposal(
        incident_id=incident.id, method="connectorInstances.runOnDemand",
        resource=resource,
        resource_version=resource_version(
            stack["deployer"].get(resource)
        ),
    )
    stack["approvals"].approve(incident.id, proposal.hash, "op")
    stack["executor"].apply(incident, proposal)
    journal = Journal()
    journal.append(stack["journal"].records()[0])
    recovered = Executor(
        stack["deployer"], stack["approvals"], journal,
        clock=stack["clock"],
    )
    results = recovered.recover()
    assert len(results) == 1
    assert results[0].outcome == "applied"
    assert results[0].api_calls == 1
    assert journal.records()[-1]["outcome"] == "applied"


def test_recover_restart_already_applied():
    """enabled=true with a moved version is done without a call (pin 19)."""
    stack = _stack()
    resource = _connector(stack["fleet"], "alpha")
    incident = _incident("alert-ingestion/alpha", "dead-connector")
    proposal = _proposal(
        incident_id=incident.id, method="connectorInstances.restart",
        resource=resource,
        resource_version=resource_version(
            stack["deployer"].get(resource)
        ),
    )
    stack["approvals"].approve(incident.id, proposal.hash, "op")
    stack["executor"].apply(incident, proposal)
    journal = Journal()
    journal.append(stack["journal"].records()[0])
    calls = len(stack["journal"].records())
    recovered = Executor(
        stack["deployer"], stack["approvals"], journal,
        clock=stack["clock"],
    )
    results = recovered.recover()
    assert len(results) == 1
    assert results[0].outcome == "already_applied"
    assert results[0].api_calls == 0
    assert stack["deployer"].get(resource)["enabled"] is True
    assert calls == 5  # the original apply is untouched


def test_failed_step_writes_error_terminal():
    """A 400 mid-apply writes terminal error and raises ApplyError."""
    stack = _stack()
    resource = _job(stack["fleet"], "alpha")
    incident = _incident("intel-sync/alpha", "dead-job")
    proposal = _proposal(
        incident_id=incident.id, method="jobInstances.patch",
        resource=resource, update_mask=["intervalSeconds"],
        patch={"intervalSeconds": 30},
        resource_version=resource_version(
            stack["deployer"].get(resource)
        ),
    )
    stack["approvals"].approve(incident.id, proposal.hash, "op")
    with pytest.raises(ApplyError):
        stack["executor"].apply(incident, proposal)
    records = stack["journal"].records()
    assert [r["type"] for r in records] == [
        "intent", "started", "terminal",
    ]
    assert records[-1]["outcome"] == "error"
    assert records[-1]["detail"] != ""


def _verifier_stack() -> dict[str, Any]:
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / "alert-ingestion.json")
    clock = SimClock(T0)
    deployments = deployments_from_fleet(fleet, contract)
    evaluator = Evaluator([contract], deployments, clock)
    registry = Registry([contract], deployments)
    return {
        "evaluator": evaluator, "registry": registry, "clock": clock,
        "verifier": Verifier(evaluator, registry, clock),
    }


def _diagnosis(tripwire: str, seconds: int) -> dict[str, Any]:
    return {"expectedVerification": {
        "tripwire": tripwire, "condition": "recovers_within",
        "seconds": seconds,
    }}


def test_verifier_pending_verified_failed():
    """Status-based check with a deadline (d6a-pins 15)."""
    parts = _verifier_stack()
    clock = parts["clock"]
    verifier = parts["verifier"]
    incident = _incident("alert-ingestion/alpha", "dead-connector")
    verifier.start(incident, _diagnosis("dead-connector", 60),
                   changed_resource=None)
    assert verifier.active() == [incident.id]
    assert verifier.check(incident.id) == "verified"
    assert verifier.active() == []

    verifier.start(incident, _diagnosis("dead-connector", 60),
                   changed_resource=None)
    parts["evaluator"].enter_change_point(
        "alert-ingestion/alpha", "connector", duration_s=3600,
        overrides={"dead-connector": {"maxSilentIntervals": 0}},
    )
    clock.advance(61)
    parts["evaluator"].tick()
    assert verifier.check(incident.id) == "failed"
    assert verifier.active() == []


def test_verifier_arms_change_point():
    """With a changed resource, the component's change point arms (pin 15)."""
    parts = _verifier_stack()
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    resource = _connector(fleet, "alpha")
    incident = _incident("alert-ingestion/alpha", "dead-connector")
    entered: list[dict[str, Any]] = []
    evaluator = parts["evaluator"]
    orig = evaluator.enter_change_point

    def spy(deployment: str, component: str, **kwargs: Any) -> None:
        entered.append({
            "deployment": deployment, "component": component, **kwargs,
        })
        return orig(deployment, component, **kwargs)

    evaluator.enter_change_point = spy  # type: ignore[method-assign]
    parts["verifier"].start(
        incident, _diagnosis("dead-connector", 60),
        changed_resource=resource,
    )
    assert entered == [{
        "deployment": "alert-ingestion/alpha", "component": "connector",
        "duration_s": 1800,
        "overrides": {"dead-connector": {"maxSilentIntervals": 2}},
    }]


def _investigator_stack(responses: list[dict[str, Any]]) -> dict[str, Any]:
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / "alert-ingestion.json")
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(fleet, 11, clock, sink)
    http = TestClient(create_app(world, dev_auth=True))
    deployments = deployments_from_fleet(fleet, contract)
    registry = Registry([contract], deployments)
    tools = ToolSet(
        MemoryLogQuery(sink),
        ChronicleHttpClient(http, "obs-reader"),
        MemoryStateStore(), registry, clock=clock, verification=True,
    )
    transport = ScriptedTransport(responses)
    config = GatewayConfig(
        roles={"investigate": RoleConfig(
            model="test-model", reasoning_effort="medium")},
        base_url="https://api.meta.ai/v1",
        prices={"test-model": {
            "input": 1.0, "cached_input": 0.5, "output": 2.0,
        }},
    )
    return {
        "tools": tools, "registry": registry, "clock": clock,
        "transport": transport,
        "gateway": ModelGateway(config, transport),
        "store": MemorySessionStore(),
    }


def _stall(n: int) -> list[dict[str, Any]]:
    return [{
        "output": [
            {"type": "reasoning", "id": f"rs_{i:03d}",
             "encrypted_content": "enc"},
            {"type": "function_call", "call_id": f"call_{i:03d}",
             "name": "update_plan",
             "arguments": json.dumps(
                 {"plan": [], "rationale": f"stall {i}"})},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 10,
                  "input_tokens_details": {"cached_tokens": 50},
                  "output_tokens_details": {"reasoning_tokens": 5}},
    } for i in range(1, n + 1)]


def test_investigator_budgets_and_reopen_gate():
    """Investigator wires budgets; reopen needs submitted (d6a-pins 1)."""
    parts = _investigator_stack(_stall(3))
    incident = _incident("alert-ingestion/alpha", "dead-connector")
    investigator = Investigator(
        parts["tools"], parts["gateway"], parts["store"],
        max_turns=1, resume_turns=2,
    )
    assert investigator.tools is parts["tools"]
    first = investigator.start(incident)
    assert (first.status, first.reason) == ("needs_input", "budget")
    assert len(parts["transport"].requests) == 1
    with pytest.raises(ValueError):
        reopen(
            incident, parts["tools"], parts["gateway"], parts["store"],
            message="nope",
        )
    second = investigator.resume(incident, message="go on")
    assert (second.status, second.reason) == ("needs_input", "budget")
    assert len(parts["transport"].requests) == 3
    with pytest.raises(ValueError):
        investigator.reopen(incident, message="again")
    assert len(parts["transport"].requests) == 3


class _StubInvestigator:
    """Investigator double returning one submitted restart proposal."""

    def __init__(self, tools: Any, resource: str) -> None:
        self.tools = tools
        self._resource = resource

    def start(self, incident: Any) -> InvestigationResult:
        return InvestigationResult(
            diagnosis={
                "incidentId": incident.id,
                "deployment": incident.deployment,
                "component": "connector",
                "category": "connector_stalled",
                "hypothesis": "H1",
                "summary": "The connector stalled.",
                "evidence": [],
                "action": {
                    "type": "propose",
                    "method": "connectorInstances.restart",
                    "resource": self._resource,
                    "updateMask": [],
                    "patch": {},
                },
                "expectedVerification": {
                    "tripwire": "dead-connector",
                    "condition": "recovers_within",
                    "seconds": 180,
                },
                "confidence": "high",
            },
            status="submitted",
            reason="propose",
            transcript_id="tx-stub",
            turns=1,
            read_calls=0,
        )


class _StubExecutor:
    def __init__(self) -> None:
        self.applied: list[str] = []

    def apply(self, incident: Any, proposal: Any) -> ApplyResult:
        self.applied.append(proposal.hash)
        return ApplyResult(
            proposal_hash=proposal.hash,
            outcome="applied",
            api_calls=2,
            resource_after={},
        )


class _StubVerifier:
    def start(
        self, incident: Any, diagnosis: Any, *, changed_resource: Any
    ) -> None:
        pass

    def check(self, incident_id: str) -> str:
        return "pending"


class _StubEvaluator:
    def status(self) -> dict[str, dict[str, str]]:
        return {}


def _manager_stack() -> dict[str, Any]:
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / "alert-ingestion.json")
    clock = SimClock(T0)
    deployments = deployments_from_fleet(fleet, contract)
    registry = Registry([contract], deployments)
    tools = ToolSet(
        MemoryLogQuery(MemorySink()), None, MemoryStateStore(),
        registry, clock=clock, verification=True,
    )
    world = World(fleet, 11, clock, MemorySink())
    http = TestClient(create_app(world, dev_auth=True))
    resource = _connector(fleet, "alpha")
    manager = IncidentManager(
        _StubEvaluator(), _StubInvestigator(tools, resource),
        ApprovalStore(clock), _StubExecutor(), _StubVerifier(),
        MemoryStateStore(), clock, registry=registry,
        reader=ChronicleHttpClient(http, "obs-reader"),
    )
    return {"manager": manager, "clock": clock, "fleet": fleet}


def test_applied_precedes_verification_started():
    """Review fix: postmortems read applied -> verification_started."""
    parts = _manager_stack()
    manager = parts["manager"]
    manager.on_trip(TripEvent(
        kind="trip", deployment="alert-ingestion/alpha",
        tripwire="dead-connector", at=T0, reason="probe", evidence=(),
    ))
    manager.tick()
    record = manager.incidents()[0]
    assert record.state == IncidentState.AWAITING_APPROVAL
    manager.approve(record.id, record.proposals[-1].hash, by="op")
    manager.tick()
    kinds = [event.kind for event in manager.get(record.id).timeline]
    assert kinds.index("applied") < kinds.index("verification_started")


def test_verification_is_read_only():
    """ToolSet.verification has no setter (d6a-pins 1)."""
    parts = _investigator_stack([])
    assert parts["tools"].verification is True
    with pytest.raises(AttributeError):
        parts["tools"].verification = False  # type: ignore[misc]
