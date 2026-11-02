"""D6 acceptance: fix loop E2E, NEEDS_INPUT handling, postmortem, gates-on."""

import pytest

from d6_helpers import (
    TICK,
    at_offset,
    build_e2e,
    job_name,
    last_user_text,
    step_until_incidents,
    step_until_state,
    update_plan_script,
)


def test_d6_t5_verification():
    """D6-T5: change point armed; miss -> retry; second miss -> ESCALATED.

    F1 world (first trip dead-connector +1995 s, pre-flighted with the D4
    loop). After each restart the stall is re-injected in the same tick, so
    dead-connector never recovers within the 180 s expectedVerification
    (pre-flight §4: no recovery, canary only at +2595 s). Apply 1 at +2010
    fails at +2190, the F1-retry reopen proposes again, apply 2 at +2220
    fails at +2400 -> ESCALATED with a postmortem.
    """
    from fleetsre.control.incidents import IncidentState
    from fleetsre.mock.world import FaultSpec

    e2e = build_e2e("F1", "F1-retry")
    manager = e2e["manager"]
    transport = e2e["transport"]
    entered: list[dict] = []
    evaluator = e2e["evaluator"]
    orig = evaluator.enter_change_point

    def spy(deployment, component, *, duration_s, overrides):
        entered.append({
            "deployment": deployment, "component": component,
            "duration_s": duration_s, "overrides": overrides,
        })
        return orig(
            deployment, component, duration_s=duration_s,
            overrides=overrides,
        )

    evaluator.enter_change_point = spy  # type: ignore[method-assign]
    step_until_incidents(e2e, 1, 1995 + TICK)
    incident_id = manager.incidents()[0].id
    assert incident_id == e2e["fixture"]["incident"]["id"]
    record = step_until_state(
        e2e, incident_id, IncidentState.AWAITING_APPROVAL, 1995 + TICK
    )
    manager.approve(incident_id, record.proposals[-1].hash, by="op")
    step_until_state(e2e, incident_id, IncidentState.VERIFYING, 2010 + TICK)
    e2e["world"].inject(
        FaultSpec(type="connector_stall", environment="alpha")
    )
    record = step_until_state(
        e2e, incident_id, IncidentState.AWAITING_APPROVAL, 2220
    )
    assert record.failed_attempts == 1
    assert len(transport.requests) == 12
    message = last_user_text(transport.requests[9])
    assert message.startswith(
        "Verification failed: dead-connector on alert-ingestion/alpha "
        "was not ok within 180 s of "
    )
    assert message.endswith("Investigate again.")
    assert len(record.proposals) == 2
    manager.approve(incident_id, record.proposals[-1].hash, by="op")
    step_until_state(e2e, incident_id, IncidentState.VERIFYING, 2220 + TICK)
    e2e["world"].inject(
        FaultSpec(type="connector_stall", environment="alpha")
    )
    record = step_until_state(
        e2e, incident_id, IncidentState.ESCALATED, 2400 + TICK
    )
    assert record.failed_attempts == 2
    assert len(transport.requests) == 12
    kinds = [event.kind for event in record.timeline]
    assert kinds.count("verification_failed") == 2
    assert record.postmortem is not None
    assert record.postmortem.outcome == "escalated"
    assert entered == [
        {"deployment": "alert-ingestion/alpha", "component": "connector",
         "duration_s": 1800,
         "overrides": {"dead-connector": {"maxSilentIntervals": 2}}},
        {"deployment": "alert-ingestion/alpha", "component": "connector",
         "duration_s": 1800,
         "overrides": {"dead-connector": {"maxSilentIntervals": 2}}},
    ]


def test_d6_t6_e2e_f1():
    """D6-T6: F1 stall -> restart -> verified -> RESOLVED.

    First trip dead-connector +1995 s (D4 pre-flight; the incident ID must
    equal the D5 fixture's). Approval right after the trip tick applies the
    restart at +2010; the next grid run (+2040) recovers dead-connector
    (pre-flight §3d: recovery at the next heartbeat) and the same tick
    verifies, so RESOLVED lands by +2040 plus one tick.
    """
    from fleetsre.control.incidents import IncidentState

    e2e = build_e2e("F1")
    manager = e2e["manager"]
    transport = e2e["transport"]
    step_until_incidents(e2e, 1, 1995 + TICK)
    incident_id = manager.incidents()[0].id
    assert incident_id == e2e["fixture"]["incident"]["id"]
    record = step_until_state(
        e2e, incident_id, IncidentState.AWAITING_APPROVAL, 1995 + TICK
    )
    assert record.proposals[-1].method == "connectorInstances.restart"
    manager.approve(incident_id, record.proposals[-1].hash, by="op")
    record = step_until_state(
        e2e, incident_id, IncidentState.RESOLVED, 2040 + TICK
    )
    assert len(transport.requests) == 9
    assert record.postmortem is not None
    assert record.postmortem.outcome == "resolved"
    assert record.postmortem.action["method"] == "connectorInstances.restart"
    kinds = [event.kind for event in record.timeline]
    for kind in (
        "opened", "segment", "proposal", "approved", "applied",
        "verification_started", "verified",
    ):
        assert kind in kinds, kinds


def test_d6_t7_e2e_f2():
    """D6-T7: F2 escalates, waits quietly, rotation resumes to RESOLVED.

    First trip connector-errors +2280 s (D4 pre-flight; incident ID must
    equal the D5 fixture's). The 1 h quiet window runs from the pause tick
    (+2280 to +5880): the canary trip at +2595 s only attaches (amended
    pin 13: no resume, snapshot untouched), so no new model call happens in
    the window. Clearing the fault ("mark rotated") recovers canary at
    +6000 without resuming (it was not tripped at the pause) and
    connector-errors at +6060 (3 clean runs; pre-flight §3a), which
    resumes; the resume submits none and the next tick verifies. No write
    call is made at any point.
    """
    from fleetsre.control.incidents import IncidentState

    e2e = build_e2e("F2", "F2-resume", record_writes=True)
    manager = e2e["manager"]
    loop = e2e["loop"]
    clock = e2e["clock"]
    transport = e2e["transport"]
    step_until_incidents(e2e, 1, 2280 + TICK)
    incident_id = manager.incidents()[0].id
    assert incident_id == e2e["fixture"]["incident"]["id"]
    record = step_until_state(
        e2e, incident_id, IncidentState.NEEDS_INPUT, 2280 + TICK
    )
    assert record.reason == "escalated"
    assert record.diagnosis["category"] == "credential_failure"
    assert len(transport.requests) == 9
    while clock.now() < at_offset(2280 + 3600):
        loop.step()
    record = manager.get(incident_id)
    assert len(transport.requests) == 9
    assert record.state == IncidentState.NEEDS_INPUT
    assert "canary" in record.tripwires
    attached = [event for event in record.timeline if event.kind == "attached"]
    assert len(attached) == 1
    assert attached[0].detail["tripwire"] == "canary"
    assert record.paused_snapshot == {"connector-errors": "tripped"}
    faults = e2e["world"].list_faults()
    assert len(faults) == 1
    e2e["world"].clear(faults[0]["id"])
    while clock.now() < at_offset(6045):
        loop.step()
    assert len(transport.requests) == 9
    assert manager.get(incident_id).state == IncidentState.NEEDS_INPUT
    record = step_until_state(
        e2e, incident_id, IncidentState.RESOLVED, 6075 + TICK
    )
    assert len(transport.requests) == 12
    resume_message = last_user_text(transport.requests[9])
    assert "canary none -> tripped" in resume_message
    assert "connector-errors tripped -> ok" in resume_message
    assert record.diagnosis["action"]["type"] == "none"
    assert len(manager.incidents()) == 1
    for recorder in e2e["recorders"]:
        assert recorder.writes == []


def test_d6_t8_e2e_f3():
    """D6-T8: F3 severity drift -> parameter patch -> RESOLVED.

    First trip priority-shape +1920 s (D4 pre-flight; incident ID must
    equal the D5 fixture's). Approval is held until +1965 so the apply
    lands at +1980, exactly the pre-flighted patch time (§3b), whose
    recovery (+2580, 20 valid entries) plus one tick bounds RESOLVED.
    """
    from fleetsre.control.incidents import IncidentState

    e2e = build_e2e("F3")
    manager = e2e["manager"]
    loop = e2e["loop"]
    clock = e2e["clock"]
    transport = e2e["transport"]
    step_until_incidents(e2e, 1, 1920 + TICK)
    incident_id = manager.incidents()[0].id
    assert incident_id == e2e["fixture"]["incident"]["id"]
    step_until_state(
        e2e, incident_id, IncidentState.AWAITING_APPROVAL, 1920 + TICK
    )
    while clock.now() < at_offset(1965):
        loop.step()
    record = manager.get(incident_id)
    assert record.state == IncidentState.AWAITING_APPROVAL
    assert len(transport.requests) == 9
    manager.approve(incident_id, record.proposals[-1].hash, by="op")
    record = step_until_state(
        e2e, incident_id, IncidentState.RESOLVED, 2580 + TICK
    )
    assert len(transport.requests) == 9
    patch = record.postmortem.action["patch"]
    values = {
        p["displayName"]: p["value"] for p in patch["parameters"]
    }
    assert values["Unknown Severity Priority"] == "100"
    applied_at = next(
        event.at for event in record.timeline if event.kind == "applied"
    )
    registry = e2e["registry"]
    ingested = [
        entry for entry in e2e["sink"].entries
        if entry.timestamp > applied_at
        and entry.jsonPayload.get("event") == "alert.ingested"
        and any(
            dep == "alert-ingestion/alpha"
            for dep, _component in registry.route(entry)
        )
    ]
    assert len(ingested) >= 1
    assert all(
        isinstance(entry.jsonPayload["alert"]["priority"], int)
        for entry in ingested
    )


def test_d6_t9_e2e_f4():
    """D6-T9: F4 hollow success -> Lookback Hours patch -> RESOLVED.

    First trip hollow-run +2700 s (D4 pre-flight; incident ID must equal
    the D5 fixture's). Approval right after the trip tick applies at +2715,
    before the next job runs; recovery lands at +3300 (2 valid runs,
    pre-flight §3c) and the same tick verifies.
    """
    from fleetsre.control.incidents import IncidentState

    e2e = build_e2e("F4")
    manager = e2e["manager"]
    transport = e2e["transport"]
    step_until_incidents(e2e, 1, 2700 + TICK)
    incident_id = manager.incidents()[0].id
    assert incident_id == e2e["fixture"]["incident"]["id"]
    record = step_until_state(
        e2e, incident_id, IncidentState.AWAITING_APPROVAL, 2700 + TICK
    )
    manager.approve(incident_id, record.proposals[-1].hash, by="op")
    record = step_until_state(
        e2e, incident_id, IncidentState.RESOLVED, 3300 + TICK
    )
    assert len(transport.requests) == 9
    seen = e2e["deployer"].get(job_name(e2e["fleet"], "alpha"))
    values = {
        p["displayName"]: p["value"] for p in seen["parameters"]
    }
    assert values["Lookback Hours"] == "24"
    assert record.postmortem.outcome == "resolved"


def _f2_to_resolved() -> tuple:
    """Drive the T7 flow to RESOLVED; returns (e2e, incident_id)."""
    from fleetsre.control.incidents import IncidentState

    e2e = build_e2e("F2", "F2-resume")
    manager = e2e["manager"]
    loop = e2e["loop"]
    clock = e2e["clock"]
    step_until_incidents(e2e, 1, 2280 + TICK)
    incident_id = manager.incidents()[0].id
    step_until_state(e2e, incident_id, IncidentState.NEEDS_INPUT, 2280 + TICK)
    while clock.now() < at_offset(2280 + 3600):
        loop.step()
    faults = e2e["world"].list_faults()
    e2e["world"].clear(faults[0]["id"])
    step_until_state(e2e, incident_id, IncidentState.RESOLVED, 6075 + TICK)
    return e2e, incident_id


def test_d6_t10_postmortem():
    """D6-T10: RESOLVED and ESCALATED incidents carry full postmortems.

    The RESOLVED case replays the T7 flow (one pause, one attach, one
    resume); the ESCALATED case closes an escalated F2 pause. Timings come
    from the D4 pre-flight (§1-§3a), as in T7.
    """
    from fleetsre.control.incidents import IncidentState

    e2e, incident_id = _f2_to_resolved()
    manager = e2e["manager"]
    record = manager.get(incident_id)
    postmortem = record.postmortem
    assert postmortem is not None
    assert postmortem.incident_id == incident_id
    assert postmortem.deployment == "alert-ingestion/bravo"
    assert postmortem.service == "alert-ingestion"
    assert postmortem.outcome == "resolved"
    assert postmortem.category == "credential_failure"
    assert postmortem.root_cause == record.diagnosis["summary"]
    assert postmortem.action["type"] == "none"
    assert postmortem.closed_at > postmortem.opened_at
    assert postmortem.timeline == record.timeline
    assert len(postmortem.transcript_ids) == 1
    kinds = [event.kind for event in postmortem.timeline]
    assert kinds[0] == "opened"
    assert kinds.count("segment") == 2
    assert kinds.count("needs_input") == 1
    assert kinds.count("resumed") == 1
    assert kinds.count("attached") == 1
    assert kinds.count("recovery") == 2
    assert "verification_started" in kinds
    assert "verified" in kinds
    resumed = [e for e in postmortem.timeline if e.kind == "resumed"]
    assert all(e.detail.get("trigger") == "tripwire" for e in resumed)
    assert all(e.detail.get("message") for e in resumed)

    e2e2 = build_e2e("F2")
    manager2 = e2e2["manager"]
    step_until_incidents(e2e2, 1, 2280 + TICK)
    incident2 = manager2.incidents()[0].id
    step_until_state(
        e2e2, incident2, IncidentState.NEEDS_INPUT, 2280 + TICK
    )
    manager2.close(incident2, by="op", note="handing to netops")
    record2 = manager2.get(incident2)
    assert record2.state == IncidentState.ESCALATED
    postmortem2 = record2.postmortem
    assert postmortem2 is not None
    assert postmortem2.outcome == "escalated"
    assert postmortem2.category == "credential_failure"
    assert postmortem2.action["type"] == "escalate"
    assert len(postmortem2.transcript_ids) == 1
    kinds2 = [event.kind for event in postmortem2.timeline]
    assert "needs_input" in kinds2
    assert "closed" in kinds2
    closed = next(
        e for e in postmortem2.timeline if e.kind == "closed"
    )
    assert closed.detail.get("by") == "op"
    assert closed.detail.get("note") == "handing to netops"


def test_d6_t16_needs_input_handling():
    """D6-T16: reply resumes, extra turns extend, close escalates.

    F3 worlds (first trip priority-shape +1920 s, D4 pre-flight; no second
    tripwire fires, so ticks with nothing changed must make no model call).
    The 24-turn stall script pauses with reason budget under default
    budgets; reply grants resume_turns (8) plus extra_turns.
    """
    from fleetsre.control.incidents import IncidentState

    e2e = build_e2e(
        "F3", first=update_plan_script(1, 24),
        extra_responses=update_plan_script(25, 10),
    )
    manager = e2e["manager"]
    loop = e2e["loop"]
    transport = e2e["transport"]
    step_until_incidents(e2e, 1, 1920 + TICK)
    incident_id = manager.incidents()[0].id
    record = step_until_state(
        e2e, incident_id, IncidentState.NEEDS_INPUT, 1920 + TICK
    )
    assert record.reason == "budget"
    assert len(transport.requests) == 24
    for _ in range(4):
        loop.step()
    assert len(transport.requests) == 24
    assert manager.get(incident_id).state == IncidentState.NEEDS_INPUT
    with pytest.raises(ValueError):
        manager.reply(incident_id, "   ", by="op")
    assert len(transport.requests) == 24
    manager.reply(incident_id, "operator here: keep going", by="op",
                  extra_turns=2)
    record = manager.get(incident_id)
    assert record.state == IncidentState.INVESTIGATING
    step_until_state(e2e, incident_id, IncidentState.NEEDS_INPUT, 2400)
    assert len(transport.requests) == 34
    message = last_user_text(transport.requests[24])
    assert message == "Reply from op: operator here: keep going"
    assert manager.get(incident_id).reason == "budget"

    e2e2 = build_e2e("F3", first=update_plan_script(1, 24))
    manager2 = e2e2["manager"]
    step_until_incidents(e2e2, 1, 1920 + TICK)
    incident2 = manager2.incidents()[0].id
    step_until_state(
        e2e2, incident2, IncidentState.NEEDS_INPUT, 1920 + TICK
    )
    manager2.close(incident2, by="op", note="not now")
    record2 = manager2.get(incident2)
    assert record2.state == IncidentState.ESCALATED
    assert record2.postmortem is not None
    assert record2.postmortem.outcome == "escalated"
    assert record2.postmortem.category is None
    assert record2.postmortem.action is None
    with pytest.raises(ValueError):
        manager2.close(incident2, by="op", note="again")

    e2e3 = build_e2e("F1")
    manager3 = e2e3["manager"]
    step_until_incidents(e2e3, 1, 1995 + TICK)
    incident3 = manager3.incidents()[0].id
    step_until_state(
        e2e3, incident3, IncidentState.AWAITING_APPROVAL, 1995 + TICK
    )
    with pytest.raises(ValueError):
        manager3.reply(incident3, "too late", by="op")
    with pytest.raises(ValueError):
        manager3.close(incident3, by="op", note="too late")
    assert len(e2e3["transport"].requests) == 9


def test_d6_t17_reject_with_notes():
    """D6-T17: rejection reopens with the notes; the new hash stands alone.

    F3-reject proposes Unknown Severity Priority = -1 (first trip
    priority-shape +1920 s, D4 pre-flight). Reject with notes -> the
    F3-revise reopen proposes 100 with a new hash; approving the stale hash
    fails, and an approval for it cannot apply the revised proposal. The
    revised proposal then applies and the incident RESOLVES (recovery ~600
    s after the patch, pre-flight §3b).
    """
    from fleetsre.control.executor import NotApproved
    from fleetsre.control.incidents import IncidentState

    e2e = build_e2e("F3-reject", "F3-revise")
    manager = e2e["manager"]
    transport = e2e["transport"]
    step_until_incidents(e2e, 1, 1920 + TICK)
    incident_id = manager.incidents()[0].id
    assert incident_id == e2e["fixture"]["incident"]["id"]
    record = step_until_state(
        e2e, incident_id, IncidentState.AWAITING_APPROVAL, 1920 + TICK
    )
    old = record.proposals[-1]
    assert old.seq == 1
    old_values = {
        p["displayName"]: p["value"] for p in old.patch["parameters"]
    }
    assert old_values["Unknown Severity Priority"] == "-1"
    rejection = manager.reject(
        incident_id, old.hash, by="op",
        notes="unknown severities should map to 100",
    )
    assert rejection.id == "R1"
    record = manager.get(incident_id)
    assert record.state == IncidentState.INVESTIGATING
    assert record.failed_attempts == 0
    assert record.pending is not None
    assert record.pending.trigger == "rejection"
    record = step_until_state(
        e2e, incident_id, IncidentState.AWAITING_APPROVAL, 1920 + 4 * TICK
    )
    assert len(transport.requests) == 12
    message = last_user_text(transport.requests[9])
    assert message == (
        "Rejected by op: unknown severities should map to 100 "
        "Revise the proposal or escalate."
    )
    new = record.proposals[-1]
    assert new.seq == 2
    assert new.hash != old.hash
    new_values = {
        p["displayName"]: p["value"] for p in new.patch["parameters"]
    }
    assert new_values["Unknown Severity Priority"] == "100"
    with pytest.raises(ValueError):
        manager.approve(incident_id, old.hash, by="op")
    approval = e2e["approvals"].approve(incident_id, old.hash, "op")
    assert approval.id == "A1"
    with pytest.raises(NotApproved):
        e2e["executor"].apply(manager.get(incident_id).incident, new)
    manager.approve(incident_id, new.hash, by="op")
    record = step_until_state(
        e2e, incident_id, IncidentState.RESOLVED, 3200
    )
    assert record.postmortem.action["patch"]["parameters"] is not None


def test_d6_t20_gates_always_on():
    """D6-T20: the manager refuses a ToolSet with verification off.

    No D4 numbers needed: constructor gates (d6a-pins 8), also covering the
    obs-reader check on the reader client.
    """
    from fastapi.testclient import TestClient

    from fleetsre.adapters.chronicle_http import ChronicleHttpClient
    from fleetsre.clock import SimClock
    from fleetsre.control.approvals import ApprovalStore
    from fleetsre.control.executor import Executor
    from fleetsre.control.incidents import IncidentManager
    from fleetsre.control.journal import Journal
    from fleetsre.control.verify import Verifier
    from fleetsre.evaluator.engine import Evaluator
    from fleetsre.mock.chronicle_api import create_app
    from fleetsre.mock.world import World
    from fleetsre.ports import (
        MemoryLogQuery,
        MemorySink,
        MemoryStateStore,
    )
    from fleetsre.registry import Registry, deployments_from_fleet
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import Investigator
    from fleetsre.sre.session import MemorySessionStore
    from fleetsre.sre.tools import ToolSet

    from d6_helpers import T0, contract, fleet, gateway_config

    flt = fleet()
    cont = contract("alert-ingestion")
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(flt, 21, clock, sink)
    deployments = deployments_from_fleet(flt, cont)
    evaluator = Evaluator([cont], deployments, clock)
    registry = Registry([cont], deployments)
    http = TestClient(create_app(world, dev_auth=True))
    log_query = MemoryLogQuery(sink)
    state = MemoryStateStore()
    tools = ToolSet(
        log_query, ChronicleHttpClient(http, "obs-reader"), state,
        registry, clock=clock, verification=False,
    )
    gateway = ModelGateway(gateway_config(), ScriptedTransport([]))
    store = MemorySessionStore()
    investigator = Investigator(tools, gateway, store)
    approvals = ApprovalStore(clock)
    journal = Journal()
    executor = Executor(
        ChronicleHttpClient(http, "deployer"), approvals, journal,
        clock=clock,
    )
    verifier = Verifier(evaluator, registry, clock)
    with pytest.raises(ValueError):
        IncidentManager(
            evaluator, investigator, approvals, executor, verifier,
            state, clock, registry=registry,
            reader=ChronicleHttpClient(http, "obs-reader"),
        )
    tools_on = ToolSet(
        log_query, ChronicleHttpClient(http, "obs-reader"), state,
        registry, clock=clock, verification=True,
    )
    investigator_on = Investigator(tools_on, gateway, store)
    with pytest.raises(ValueError):
        IncidentManager(
            evaluator, investigator_on, approvals, executor, verifier,
            state, clock, registry=registry,
            reader=ChronicleHttpClient(http, "deployer"),
        )
