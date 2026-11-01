"""D6 acceptance: executor, approvals, journal, crash recovery."""

import pytest

from conftest import (
    T0,
    TICK,
    at_offset,
    build_e2e,
    connector_name,
    contract,
    fleet,
    job_name,
    last_user_text,
    step_until_incidents,
    step_until_state,
)


def _world_clients(seed: int = 11):
    """Bare world + obs-reader/deployer clients (no faults)."""
    from fastapi.testclient import TestClient

    from fleetsre.adapters.chronicle_http import ChronicleHttpClient
    from fleetsre.clock import SimClock
    from fleetsre.mock.chronicle_api import create_app
    from fleetsre.mock.world import World
    from fleetsre.ports import MemorySink

    flt = fleet()
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(flt, seed, clock, sink)
    http = TestClient(create_app(world, dev_auth=True))
    return {
        "fleet": flt, "clock": clock, "sink": sink, "world": world,
        "obs": ChronicleHttpClient(http, "obs-reader"),
        "deployer": ChronicleHttpClient(http, "deployer"),
    }


def _proposal(**overrides):
    """Proposal record with dummy defaults (d6a-pins 3 fields)."""
    from fleetsre.control.proposals import Proposal

    base = {
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


def _incident_for(service_env, tripwire, at):
    from fleetsre.evaluator.engine import TripEvent
    from fleetsre.registry import Registry, deployments_from_fleet
    from fleetsre.sre.schemas import Incident

    flt = fleet()
    cont = contract(service_env.split("/", 1)[0])
    registry = Registry(
        [cont], deployments_from_fleet(flt, cont)
    )
    event = TripEvent(
        kind="trip", deployment=service_env, tripwire=tripwire, at=at,
        reason="d6 unit probe", evidence=(),
    )
    return Incident.from_trip(event, registry, at)


def test_d6_t1_approval_required():
    """D6-T1: apply without an approval, or for another hash, is NotApproved.

    No D4 numbers needed: the approval gate (d6a-pins 18 step 1) fires
    before any resource read.
    """
    from fleetsre.control.approvals import ApprovalStore
    from fleetsre.control.executor import Executor, NotApproved
    from fleetsre.control.journal import Journal

    parts = _world_clients()
    approvals = ApprovalStore(parts["clock"])
    journal = Journal()
    executor = Executor(
        parts["deployer"], approvals, journal, clock=parts["clock"]
    )
    incident = _incident_for(
        "alert-ingestion/alpha", "dead-connector", at_offset(1995)
    )
    proposal = _proposal(
        incident_id=incident.id, resource=connector_name(parts["fleet"], "alpha")
    )
    with pytest.raises(NotApproved):
        executor.apply(incident, proposal)
    approvals.approve(incident.id, "some-other-hash", "op")
    with pytest.raises(NotApproved):
        executor.apply(incident, proposal)
    assert journal.records() == []


def test_d6_t2_concurrency():
    """D6-T2: a version move between proposal and apply is a Conflict.

    Direct part: apply raises Conflict carrying resource/expected/actual.
    Manager part (F1 world; first trip dead-connector +1995 s pre-flighted
    with the D4 loop): approve, bump the version with a displayName patch,
    tick -> Conflict -> INVESTIGATING with a conflict reopen pending (not a
    failed attempt); the retry proposes again and the incident RESOLVES.
    """
    from fleetsre.control.approvals import ApprovalStore
    from fleetsre.control.executor import Conflict, Executor
    from fleetsre.control.incidents import IncidentState
    from fleetsre.control.journal import Journal
    from fleetsre.control.proposals import resource_version

    parts = _world_clients()
    resource = connector_name(parts["fleet"], "alpha")
    before = parts["deployer"].get(resource)
    version_before = resource_version(before)
    approvals = ApprovalStore(parts["clock"])
    journal = Journal()
    executor = Executor(
        parts["deployer"], approvals, journal, clock=parts["clock"]
    )
    incident = _incident_for(
        "alert-ingestion/alpha", "dead-connector", at_offset(1995)
    )
    proposal = _proposal(
        incident_id=incident.id, resource=resource,
        resource_version=version_before,
    )
    approvals.approve(incident.id, proposal.hash, "op")
    parts["deployer"].patch(
        resource,
        {"displayName": "renamed",
         "integrationVersion": before["integrationVersion"]},
        ["displayName"],
    )
    with pytest.raises(Conflict) as exc:
        executor.apply(incident, proposal)
    assert exc.value.resource == resource
    assert exc.value.expected == version_before
    assert exc.value.actual == resource_version(
        parts["deployer"].get(resource)
    )
    assert exc.value.actual != version_before

    e2e = build_e2e("F1", "F1-retry")
    manager = e2e["manager"]
    loop = e2e["loop"]
    clock = e2e["clock"]
    transport = e2e["transport"]
    resource = connector_name(e2e["fleet"], "alpha")
    step_until_incidents(e2e, 1, 1995 + TICK)
    incident_id = manager.incidents()[0].id
    assert incident_id == e2e["fixture"]["incident"]["id"]
    record = step_until_state(
        e2e, incident_id, IncidentState.AWAITING_APPROVAL, 1995 + TICK
    )
    old_hash = record.proposals[-1].hash
    old_version = record.proposals[-1].resource_version
    manager.approve(incident_id, old_hash, by="op")
    assert len(transport.requests) == 9
    current = e2e["deployer"].get(resource)
    e2e["deployer"].patch(
        resource,
        {"displayName": "renamed",
         "integrationVersion": current["integrationVersion"]},
        ["displayName"],
    )
    loop.step()
    record = manager.get(incident_id)
    assert record.state == IncidentState.INVESTIGATING
    assert record.failed_attempts == 0
    assert record.pending is not None
    assert record.pending.trigger == "conflict"
    assert len(transport.requests) == 9
    kinds = [event.kind for event in record.timeline]
    assert "conflict" in kinds
    loop.step()
    record = manager.get(incident_id)
    assert record.state == IncidentState.AWAITING_APPROVAL
    assert len(transport.requests) == 12
    message = last_user_text(transport.requests[9])
    assert message.startswith("Conflict: ") and resource in message
    assert old_version in message
    assert record.proposals[-1].hash != old_hash
    assert record.proposals[-1].seq == 2
    manager.approve(incident_id, record.proposals[-1].hash, by="op")
    step_until_state(e2e, incident_id, IncidentState.RESOLVED, 3600)
    assert clock.now() <= at_offset(3600)


def test_d6_t3_deployer_identity():
    """D6-T3: the executor needs deployer; obs-reader writes get 403.

    No D4 numbers needed: identity checks, not timing.
    """
    from fastapi.testclient import TestClient

    from fleetsre.adapters.chronicle_http import ChronicleHttpClient
    from fleetsre.clock import SimClock
    from fleetsre.control.approvals import ApprovalStore
    from fleetsre.control.executor import Executor
    from fleetsre.control.journal import Journal
    from fleetsre.mock.chronicle_api import create_app
    from fleetsre.mock.world import World
    from fleetsre.ports import ChronicleError, MemorySink

    flt = fleet()
    clock = SimClock(T0)
    world = World(flt, 12, clock, MemorySink())
    http = TestClient(create_app(world, dev_auth=True))
    obs = ChronicleHttpClient(http, "obs-reader")
    deployer = ChronicleHttpClient(http, "deployer")
    resource = connector_name(flt, "alpha")
    seen = deployer.get(resource)
    body = {"enabled": seen["enabled"],
            "integrationVersion": seen["integrationVersion"]}
    with pytest.raises(ChronicleError) as exc:
        obs.patch(resource, body, ["enabled"])
    assert exc.value.status == 403
    assert exc.value.body.get("status") == "PERMISSION_DENIED"
    approvals = ApprovalStore(clock)
    journal = Journal()
    Executor(deployer, approvals, journal, clock=clock)
    with pytest.raises(ValueError):
        Executor(obs, approvals, journal, clock=clock)


def test_d6_t4_restart_macro():
    """D6-T4: restart issues enabled=false then enabled=true, in order.

    Connector and job restarts on a bare world, observed with a recording
    wrapper around the deployer client (d6a-pins 25). No D4 numbers needed.
    """
    from conftest import RecordingChronicle

    from fleetsre.control.approvals import ApprovalStore
    from fleetsre.control.executor import Executor
    from fleetsre.control.journal import Journal
    from fleetsre.control.proposals import resource_version

    parts = _world_clients()
    flt = parts["fleet"]
    deployer = RecordingChronicle(parts["deployer"])
    approvals = ApprovalStore(parts["clock"])
    journal = Journal()
    executor = Executor(deployer, approvals, journal, clock=parts["clock"])
    seen_bodies: list[dict] = []
    inner_patch = deployer.inner.patch

    def spy(name, body, mask):
        seen_bodies.append({"name": name, "body": body, "mask": mask})
        return inner_patch(name, body, mask)

    deployer.inner.patch = spy  # type: ignore[method-assign]
    cases = [
        ("alert-ingestion/alpha", "dead-connector",
         connector_name(flt, "alpha"), "connectorInstances.restart"),
        ("intel-sync/alpha", "dead-job",
         job_name(flt, "alpha"), "jobInstances.restart"),
    ]
    for deployment, tripwire, resource, method in cases:
        incident = _incident_for(deployment, tripwire, at_offset(1995))
        proposal = _proposal(
            incident_id=incident.id, seq=1,
            hash=f"hash-{method}", method=method, resource=resource,
            resource_version=resource_version(deployer.get(resource)),
        )
        approvals.approve(incident.id, proposal.hash, "op")
        before = len(seen_bodies)
        result = executor.apply(incident, proposal)
        assert result.outcome == "applied"
        assert result.api_calls == 2
        calls = seen_bodies[before:]
        assert [c["body"]["enabled"] for c in calls] == [False, True]
        assert all(c["mask"] == ["enabled"] for c in calls)
        assert all(c["name"] == resource for c in calls)
    assert [c["body"]["enabled"] for c in seen_bodies] == [
        False, True, False, True,
    ]
    assert "integrationVersion" in seen_bodies[0]["body"]
    assert "integrationVersion" in seen_bodies[1]["body"]


def test_d6_t18_executor_journal():
    """D6-T18: intent (approval/hash/version) precedes any write call.

    One restart apply and one parameter-patch apply on a bare world; the
    journal must hold intent, started, step(s), terminal in order, with
    every API write happening after its intent (d6a-pins 18).
    """
    from conftest import RecordingChronicle

    from fleetsre.control.approvals import ApprovalStore
    from fleetsre.control.executor import Executor
    from fleetsre.control.journal import Journal
    from fleetsre.control.proposals import resource_version

    parts = _world_clients()
    flt = parts["fleet"]
    resource = connector_name(flt, "alpha")
    deployer = RecordingChronicle(parts["deployer"])
    approvals = ApprovalStore(parts["clock"])
    journal = Journal()
    executor = Executor(deployer, approvals, journal, clock=parts["clock"])
    order: list[str] = []
    inner_append = journal.append
    inner_patch = deployer.inner.patch

    def append_spy(record):
        order.append(f"journal:{record.get('type')}")
        return inner_append(record)

    def patch_spy(name, body, mask):
        order.append("api:patch")
        return inner_patch(name, body, mask)

    journal.append = append_spy  # type: ignore[method-assign]
    deployer.inner.patch = patch_spy  # type: ignore[method-assign]
    incident = _incident_for(
        "alert-ingestion/alpha", "dead-connector", at_offset(1995)
    )
    restart = _proposal(
        incident_id=incident.id, seq=1, hash="hash-restart",
        method="connectorInstances.restart", resource=resource,
        resource_version=resource_version(deployer.get(resource)),
    )
    approvals.approve(incident.id, restart.hash, "op")
    approval_id = approvals.approval_for(restart.hash).id
    executor.apply(incident, restart)
    current = deployer.get(resource)
    params = [dict(p) for p in current["parameters"]]
    patch_proposal = _proposal(
        incident_id=incident.id, seq=2, hash="hash-patch",
        method="connectorInstances.patch", resource=resource,
        update_mask=["parameters"], patch={"parameters": params},
        resource_version=resource_version(current),
    )
    approvals.approve(incident.id, patch_proposal.hash, "op")
    executor.apply(incident, patch_proposal)
    records = journal.records()
    assert [r["type"] for r in records] == [
        "intent", "started", "step", "step", "terminal",
        "intent", "started", "step", "terminal",
    ]
    intents = [r for r in records if r["type"] == "intent"]
    assert intents[0]["proposal_hash"] == "hash-restart"
    assert intents[0]["approval_id"] == approval_id
    assert intents[0]["resource_version"] == restart.resource_version
    assert intents[1]["proposal_hash"] == "hash-patch"
    assert intents[1]["resource_version"] == patch_proposal.resource_version
    for terminal in [r for r in records if r["type"] == "terminal"]:
        assert terminal["outcome"] == "applied"
        assert terminal["proposal_hash"] in ("hash-restart", "hash-patch")
    assert order == [
        "journal:intent", "journal:started",
        "api:patch", "journal:step", "api:patch", "journal:step",
        "journal:terminal",
        "journal:intent", "journal:started",
        "api:patch", "journal:step",
        "journal:terminal",
    ]
    assert len(deployer.writes) == 3


def test_d6_t19_crash_safe_apply(tmp_path):
    """D6-T19: recover() finishes crashed applies exactly once (d6a-pins 25).

    Crash images are real journal prefixes (intent [+ started [+ step]])
    copied from completed applies; already-applied cases run against the
    world that applied them, pending cases against a same-seed rebuild.
    Covers a parameter patch (both outcomes) and a restart stopped after
    its first step (both outcomes). Patch calls are counted with a wrapper
    around the deployer client.
    """
    from conftest import RecordingChronicle

    from fleetsre.control.approvals import ApprovalStore
    from fleetsre.control.executor import Executor
    from fleetsre.control.journal import Journal
    from fleetsre.control.proposals import resource_version

    seed = 13

    def fresh_stack():
        from fastapi.testclient import TestClient

        from fleetsre.adapters.chronicle_http import ChronicleHttpClient
        from fleetsre.clock import SimClock
        from fleetsre.mock.chronicle_api import create_app
        from fleetsre.mock.world import World
        from fleetsre.ports import MemorySink

        flt = fleet()
        clock = SimClock(T0)
        world = World(flt, seed, clock, MemorySink())
        http = TestClient(create_app(world, dev_auth=True))
        deployer = RecordingChronicle(
            ChronicleHttpClient(http, "deployer")
        )
        approvals = ApprovalStore(clock)
        journal = Journal()
        executor = Executor(deployer, approvals, journal, clock=clock)
        return {
            "fleet": flt, "clock": clock, "world": world,
            "deployer": deployer, "approvals": approvals,
            "journal": journal, "executor": executor,
        }

    def value_of(deployer, resource, display):
        params = deployer.get(resource)["parameters"]
        return next(
            p["value"] for p in params if p["displayName"] == display
        )

    # Real patch apply -> crash images for the patch cases.
    stack = fresh_stack()
    resource = connector_name(stack["fleet"], "alpha")
    incident = _incident_for(
        "alert-ingestion/alpha", "priority-shape", at_offset(1920)
    )
    current = stack["deployer"].get(resource)
    params = [dict(p) for p in current["parameters"]]
    for param in params:
        if param["displayName"] == "Unknown Severity Priority":
            param["value"] = "100"
    patch_proposal = _proposal(
        incident_id=incident.id, seq=1, hash="hash-crash-patch",
        method="connectorInstances.patch", resource=resource,
        update_mask=["parameters"], patch={"parameters": params},
        resource_version=resource_version(current),
    )
    stack["approvals"].approve(incident.id, patch_proposal.hash, "op")
    stack["executor"].apply(incident, patch_proposal)
    patch_records = stack["journal"].records()
    assert [r["type"] for r in patch_records] == [
        "intent", "started", "step", "terminal",
    ]
    assert value_of(stack["deployer"], resource,
                    "Unknown Severity Priority") == "100"

    # Patch, already applied: no API call, terminal already_applied. The
    # crash journal lives on a file; recover runs on a fresh Executor over
    # a freshly loaded Journal for the same path (d6a-pins 4, 25).
    journal_path = str(tmp_path / "journal.jsonl")
    journal_done = Journal(journal_path)
    for record in patch_records[:2]:
        journal_done.append(dict(record))
    journal_reload = Journal(journal_path)
    assert [r["type"] for r in journal_reload.records()] == [
        "intent", "started",
    ]
    approvals_done = ApprovalStore(stack["clock"])
    approvals_done.approve(incident.id, patch_proposal.hash, "op")
    stack["deployer"].writes.clear()
    executor_done = Executor(
        stack["deployer"], approvals_done, journal_reload,
        clock=stack["clock"],
    )
    results = executor_done.recover()
    assert len(results) == 1
    assert results[0].outcome == "already_applied"
    assert results[0].api_calls == 0
    assert stack["deployer"].writes == []
    assert journal_reload.records()[-1]["type"] == "terminal"
    assert journal_reload.records()[-1]["outcome"] == "already_applied"
    assert value_of(stack["deployer"], resource,
                    "Unknown Severity Priority") == "100"

    # Patch, not applied: applied once on a same-seed rebuild.
    stack_pending = fresh_stack()
    journal_pending = Journal()
    journal_pending.append(dict(patch_records[0]))
    executor_pending = Executor(
        stack_pending["deployer"], stack_pending["approvals"],
        journal_pending, clock=stack_pending["clock"],
    )
    stack_pending["approvals"].approve(
        incident.id, patch_proposal.hash, "op"
    )
    assert value_of(stack_pending["deployer"], resource,
                    "Unknown Severity Priority") == ""
    results = executor_pending.recover()
    assert len(results) == 1
    assert results[0].outcome == "applied"
    assert len(stack_pending["deployer"].writes) == 1
    assert value_of(stack_pending["deployer"], resource,
                    "Unknown Severity Priority") == "100"
    assert journal_pending.records()[-1]["outcome"] == "applied"

    # Real restart apply -> crash image stopped after its first step.
    stack_r = fresh_stack()
    resource_r = connector_name(stack_r["fleet"], "alpha")
    incident_r = _incident_for(
        "alert-ingestion/alpha", "dead-connector", at_offset(1995)
    )
    restart_proposal = _proposal(
        incident_id=incident_r.id, seq=1, hash="hash-crash-restart",
        method="connectorInstances.restart", resource=resource_r,
        resource_version=resource_version(
            stack_r["deployer"].get(resource_r)
        ),
    )
    stack_r["approvals"].approve(incident_r.id, restart_proposal.hash, "op")
    stack_r["executor"].apply(incident_r, restart_proposal)
    restart_records = stack_r["journal"].records()
    assert [r["type"] for r in restart_records] == [
        "intent", "started", "step", "step", "terminal",
    ]

    # Restart, stopped after step 0 (enabled=false in the world): only the
    # second step runs.
    stack_half = fresh_stack()
    half_resource = connector_name(stack_half["fleet"], "alpha")
    seen = stack_half["deployer"].get(half_resource)
    stack_half["deployer"].patch(
        half_resource,
        {"enabled": False,
         "integrationVersion": seen["integrationVersion"]},
        ["enabled"],
    )
    assert stack_half["deployer"].get(half_resource)["enabled"] is False
    journal_half = Journal()
    for record in restart_records[:3]:
        journal_half.append(dict(record))
    executor_half = Executor(
        stack_half["deployer"], stack_half["approvals"], journal_half,
        clock=stack_half["clock"],
    )
    stack_half["approvals"].approve(
        incident_r.id, restart_proposal.hash, "op"
    )
    stack_half["deployer"].writes.clear()
    results = executor_half.recover()
    assert len(results) == 1
    assert results[0].outcome == "applied"
    assert len(stack_half["deployer"].writes) == 1
    assert stack_half["deployer"].get(half_resource)["enabled"] is True
    assert journal_half.records()[-1]["outcome"] == "applied"

    # Restart, nothing applied (enabled=true, version unchanged): both steps.
    stack_none = fresh_stack()
    none_resource = connector_name(stack_none["fleet"], "alpha")
    journal_none = Journal()
    journal_none.append(dict(restart_records[0]))
    executor_none = Executor(
        stack_none["deployer"], stack_none["approvals"], journal_none,
        clock=stack_none["clock"],
    )
    stack_none["approvals"].approve(
        incident_r.id, restart_proposal.hash, "op"
    )
    results = executor_none.recover()
    assert len(results) == 1
    assert results[0].outcome == "applied"
    assert len(stack_none["deployer"].writes) == 2
    assert stack_none["deployer"].get(none_resource)["enabled"] is True
