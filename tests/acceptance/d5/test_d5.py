"""D5 acceptance tests: SRE investigator (gated, budgeted loop)."""

import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest

from conftest import (
    READ_TOOLS,
    ROOT,
    WORKING_TOOLS,
    CountingLogQuery,
    build_incident,
    build_registry,
    build_tools,
    connector_name,
    entries_for,
    fn_call,
    gateway_config,
    job_name,
    live_gateway_config,
    ok,
    read_prompt,
    reasoning,
    refused,
    scripted_response,
    sentinel_integration_name,
    setup_from_fixture,
    sibling_of,
    trip_until,
    tripwire_of,
    usage,
    valid_hypotheses,
)

FAULTS = ("F1", "F2", "F3", "F4", "F5", "F7")
# Right answers per mocks.md §6: fault -> (category, action type).
RIGHT = {
    "F1": ("connector_stalled", "propose"),
    "F2": ("credential_failure", "escalate"),
    "F3": ("schema_drift", "propose"),
    "F4": ("job_hollow_success", "propose"),
    "F5": ("job_failing", "escalate"),
    "F7": ("job_stalled", "propose"),
}


def _session(incident_id: str) -> object:
    from fleetsre.sre.session import Session

    return Session(incident_id=incident_id)


def _set_ok(
    tools: object, session: object, call_id: str | None = None
) -> dict:
    from fleetsre.sre.tools import ToolSet

    assert isinstance(tools, ToolSet)
    return ok(tools.call(
        "set_hypotheses",
        {"items": valid_hypotheses(), "rationale": "test hypotheses"},
        session, call_id=call_id,
    ))


def _read(
    tools: object, session: object, name: str, args: dict,
    call_id: str | None = None,
) -> dict:
    from fleetsre.sre.tools import ToolSet

    assert isinstance(tools, ToolSet)
    full = dict(args)
    full.setdefault("rationale", "test read")
    return tools.call(name, full, session, call_id=call_id)


def test_d5_t1_tool_set_complete():
    """D5-T1: specs list 8+4 tools with closed schemas; reads validate."""
    from fleetsre.sre.schemas import READ_TOOL_OUTPUT_SCHEMAS

    setup = setup_from_fixture("F1")
    tools, _registry, _parts = build_tools(setup)
    specs = tools.specs()
    names = [s["name"] for s in specs]
    assert sorted(names) == sorted(READ_TOOLS | WORKING_TOOLS)
    for spec in specs:
        params = spec["parameters"]
        assert params["additionalProperties"] is False, spec["name"]
        assert "rationale" in params["required"], spec["name"]
        rat = params["properties"]["rationale"]
        assert rat.get("maxLength") == 200, spec["name"]
    submit = next(s for s in specs if s["name"] == "submit_diagnosis")
    diag = submit["parameters"]["properties"]["diagnosis"]
    for key in (
        "incidentId", "deployment", "component", "category",
        "hypothesis", "evidence", "action",
    ):
        assert key in diag["properties"], key
    record = next(s for s in specs if s["name"] == "record_evidence")
    source = record["parameters"]["properties"]["source"]
    assert source.get("anyOf") or source.get("oneOf"), source

    flt = setup["fleet"]
    service = setup["contract"].service
    env = setup["trip"].deployment.split("/", 1)[1]
    deployment = setup["trip"].deployment
    component = next(
        t.component for t in setup["contract"].tripwires
        if t.id == setup["trip"].tripwire
    )
    job_env = sorted(
        e for e, spec in flt.environments.items()
        if "intel-sync" in spec.deployments
    )[0]
    session = _session("inc-t1")
    _set_ok(tools, session)
    calls = [
        ("query_logs", {
            "deployment": deployment, "window": {"lastMinutes": 60},
            "filter": {},
        }),
        ("get_resource", {"name": connector_name(flt, env)}),
        ("list_connector_logs", {"name": connector_name(flt, env)}),
        ("list_job_logs", {"name": job_name(flt, job_env)}),
        ("test_integration_instance", {
            "name": sentinel_integration_name(flt, env),
        }),
        ("compare_siblings", {
            "service": service, "component": component,
            "window": {"lastMinutes": 60},
        }),
        ("tripwire_history", {"deployment": deployment}),
        ("get_contract", {"service": service}),
    ]
    for name, args in calls:
        result = ok(_read(tools, session, name, args))
        schema = READ_TOOL_OUTPUT_SCHEMAS[name]
        jsonschema.Draft202012Validator(schema).validate(result)
    refused(_read(tools, session, "query_logs", {
        "deployment": deployment, "window": {"lastMinutes": 0},
        "filter": {},
    }), "args.schema")
    refused(_read(tools, session, "query_logs", {
        "deployment": deployment, "window": {"lastMinutes": 10},
        "filter": {"bogus": 1},
    }), "args.schema")


def test_d5_t2_tools_are_read_only():
    """D5-T2: obs-reader identity; crafted writes refuse; snapshot kept."""
    from fleetsre.mock.auth import READER

    setup = setup_from_fixture("F1")
    tools, _registry, parts = build_tools(setup)
    assert parts["chronicle"].identity == READER
    flt = setup["fleet"]
    env = setup["trip"].deployment.split("/", 1)[1]
    conn = connector_name(flt, env)
    session = _session("inc-t2")
    _set_ok(tools, session)
    before = setup["world"].snapshot()
    for bad in (f"{conn}:runOnDemand", f"{conn}/logs/log-00000001"):
        refused(_read(tools, session, "get_resource", {"name": bad}),
                "tool.name")
    refused(tools.call(
        "patch", {"rationale": "crafted write attempt"}, session
    ), "tool.unknown")
    assert setup["world"].snapshot() == before


def test_d5_t3_loop_shape():
    """D5-T3: Responses flags, history growth, first-call-only, no-call."""
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    incident = build_incident(setup, registry)
    store = MemorySessionStore()
    prompt = read_prompt()
    deployment = setup["trip"].deployment
    multi = {
        "output": [
            reasoning(1),
            fn_call("query_logs", {
                "deployment": deployment,
                "window": {"lastMinutes": 10}, "filter": {},
                "rationale": "first of two",
            }, "call_001a"),
            fn_call("get_resource", {
                "name": "projects/x", "rationale": "second of two",
            }, "call_001b"),
        ],
        "usage": usage(1),
    }
    nocall = {"output": [reasoning(2)], "usage": usage(2)}
    single = scripted_response(3, "update_plan", {
        "plan": [], "rationale": "third turn single call",
    })
    transport = ScriptedTransport([multi, nocall, single])
    gateway = ModelGateway(gateway_config(), transport)
    result = investigate(
        incident, tools, gateway, store, max_turns=3,
        max_read_calls=12, max_tokens=400_000,
    )
    assert result.status == "needs_input"
    assert result.reason == "budget"
    requests = transport.requests
    assert len(requests) == 3
    for request in requests:
        assert request["store"] is False
        assert request["parallel_tool_calls"] is False
        assert "reasoning.encrypted_content" in request["include"]
        assert request["reasoning"] == {"effort": "medium"}
        assert request["tools"] == tools.specs()
    first_input = requests[0]["input"]
    assert first_input[0] == {
        "role": "developer", "content": prompt,
    }
    assert first_input[1]["role"] == "user"
    assert incident.id in first_input[1]["content"]
    second_input = requests[1]["input"]
    assert second_input[: len(first_input)] == first_input
    rest = second_input[len(first_input):]
    assert rest[: len(multi["output"])] == multi["output"]
    outputs = rest[len(multi["output"]):]
    assert len(outputs) == 2
    assert {o["call_id"] for o in outputs} == {
        "call_001a", "call_001b",
    }
    second_out = next(
        o for o in outputs if o["call_id"] == "call_001b"
    )
    assert "not run: only the first tool call in a response runs" in (
        second_out["output"]
    )
    first_out = next(
        o for o in outputs if o["call_id"] == "call_001a"
    )
    raw = first_out["output"]
    assert isinstance(raw, str)
    payload = json.loads(raw)
    assert payload["callId"] == "call_001a"
    assert "error" not in payload["result"]
    assert "entries" in payload["result"]
    third_input = requests[2]["input"]
    assert third_input[: len(second_input)] == second_input
    tail = third_input[len(second_input):]
    assert tail[: len(nocall["output"])] == nocall["output"]
    assert tail[len(nocall["output"]):] == [
        {"role": "user", "content": (
            "Plain text is not accepted. Call exactly one tool.")},
        {"role": "user", "content": (
            "This is your last turn. Call submit_diagnosis "
            "now with what you have.")},
    ]


def test_d5_t4_order_gate():
    """D5-T4: evidence/submission before hypotheses refuse; loop goes on."""
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    incident = build_incident(setup, registry)
    session = _session(incident.id)
    refused(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": "nope"},
        "why": "too early", "rationale": "order probe",
    }, session), "order")
    refused(tools.call("submit_diagnosis", {
        "diagnosis": {"incidentId": incident.id},
        "rationale": "order probe",
    }, session), "order")
    _set_ok(tools, session)

    store = MemorySessionStore()
    bad_first = scripted_response(1, "record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": "missing-id"},
        "why": "too early", "rationale": "order probe",
    })
    good_hyp = scripted_response(2, "set_hypotheses", {
        "items": valid_hypotheses(), "rationale": "recover",
    })
    plan = scripted_response(3, "update_plan", {
        "plan": [], "rationale": "keep going",
    })
    transport = ScriptedTransport([bad_first, good_hyp, plan])
    gateway = ModelGateway(gateway_config(), transport)
    result = investigate(
        incident, tools, gateway, store, max_turns=3,
        max_read_calls=12, max_tokens=400_000,
    )
    assert result.status == "needs_input"
    assert result.reason == "budget"
    transcript = store.transcript(result.transcript_id)
    tool_records = [r for r in transcript if r.get("tool")]
    assert [r.get("verdict") for r in tool_records] == [
        "refused", "accepted", "accepted",
    ]
    assert any(
        p.startswith("order:")
        for p in tool_records[0].get("problems", [])
    )


def test_d5_t5_hypotheses_gate():
    """D5-T5: 2-4 with an alternative; replace drops removed evidence."""
    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    incident = build_incident(setup, registry)
    session = _session(incident.id)
    one = [{"id": "H1", "statement": "s", "wouldConfirm": "c",
            "alternative": True}]
    refused(tools.call("set_hypotheses", {
        "items": one, "rationale": "too few",
    }, session), "hypotheses.count")
    many = [
        {"id": f"H{i}", "statement": "s", "wouldConfirm": "c",
         "alternative": i == 1}
        for i in range(1, 6)
    ]
    refused(tools.call("set_hypotheses", {
        "items": many, "rationale": "too many",
    }, session), "hypotheses.count")
    no_alt = [
        {"id": "H1", "statement": "s", "wouldConfirm": "c",
         "alternative": False},
        {"id": "H2", "statement": "s", "wouldConfirm": "c",
         "alternative": False},
    ]
    refused(tools.call("set_hypotheses", {
        "items": no_alt, "rationale": "no alternative",
    }, session), "hypotheses.alternative")
    bad_id = [
        {"id": "H0", "statement": "s", "wouldConfirm": "c",
         "alternative": True},
        {"id": "H0", "statement": "s", "wouldConfirm": "c",
         "alternative": False},
    ]
    refused(tools.call("set_hypotheses", {
        "items": bad_id, "rationale": "bad ids",
    }, session), "hypotheses.id")
    _set_ok(tools, session)
    env = incident.deployment.split("/", 1)[1]
    own = entries_for(setup, env)
    insert_id = own[0].insertId
    ok(_read(tools, session, "query_logs", {
        "deployment": incident.deployment,
        "window": {"lastMinutes": 120}, "filter": {},
    }))
    ok(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": insert_id},
        "why": "stays", "rationale": "keep",
    }, session))
    ok(tools.call("record_evidence", {
        "hypothesis": "H2", "stance": "refutes",
        "source": {"insertId": insert_id},
        "why": "goes", "rationale": "drop",
    }, session))
    ok(tools.call("set_hypotheses", {
        "items": [
            {"id": "H1", "statement": "s", "wouldConfirm": "c",
             "alternative": False},
            {"id": "H3", "statement": "s", "wouldConfirm": "c",
             "alternative": True},
        ],
        "rationale": "replace set",
    }, session))
    assert "H2" not in {
        e.hypothesis for e in session.evidence.values()
    }
    assert "H1" in {
        e.hypothesis for e in session.evidence.values()
    }


def test_d5_t6_evidence_gate():
    """D5-T6: bad log/tool sources refuse naming the rule; E<n> assign."""
    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    incident = build_incident(setup, registry)
    session = _session(incident.id)
    _set_ok(tools, session, call_id="call-h")
    deployment = incident.deployment
    env = deployment.split("/", 1)[1]
    own = entries_for(setup, env)
    now = setup["clock"].now()
    past = [e for e in own if e.timestamp <= now]
    assert past
    good_id = past[-1].insertId
    refused(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": "insert-missing-000"},
        "why": "bad id", "rationale": "probe",
    }, session), "evidence.insert_id")
    sib = sibling_of(setup, deployment)
    sib_env = sib.split("/", 1)[1]
    sib_entries = entries_for(setup, sib_env)
    assert sib_entries
    refused(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": sib_entries[-1].insertId},
        "why": "sibling not queried", "rationale": "probe",
    }, session), "evidence.deployment")
    refused(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"callId": "call_999", "field": "x", "value": 1},
        "why": "bad call", "rationale": "probe",
    }, session), "evidence.call_id")
    refused(tools.call("record_evidence", {
        "hypothesis": "H9", "stance": "supports",
        "source": {"insertId": good_id},
        "why": "bad hyp", "rationale": "probe",
    }, session), "evidence.hypothesis")
    refused(_read(tools, session, "query_logs", {
        "deployment": deployment, "window": {"lastMinutes": 0},
        "filter": {},
    }, call_id="call-bad"), "args.schema")
    refused(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"callId": "call-bad", "field": "total", "value": 1},
        "why": "refused call not citable", "rationale": "probe",
    }, session), "evidence.call_id")
    refused(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"callId": "call-h", "field": "count", "value": 2},
        "why": "working call not citable", "rationale": "probe",
    }, session), "evidence.call_id")
    first = _read(tools, session, "query_logs", {
        "deployment": deployment, "window": {"lastMinutes": 120},
        "filter": {},
    }, call_id="call-q1")
    assert first["callId"] == "call-q1"
    ok(first)
    assert session.calls["call-q1"].verdict == "accepted"
    refused(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"callId": "call-q1", "field": "nope.missing",
                   "value": 1},
        "why": "bad field", "rationale": "probe",
    }, session), "evidence.field")
    total = session.calls["call-q1"].result["total"]
    refused(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"callId": "call-q1", "field": "total",
                   "value": total + 9999},
        "why": "bad value", "rationale": "probe",
    }, session), "evidence.value")
    refused(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"callId": "call-q1", "field": "total",
                   "value": str(total)},
        "why": "coerced", "rationale": "probe",
    }, session), "evidence.value")
    auto = _read(tools, session, "query_logs", {
        "deployment": deployment, "window": {"lastMinutes": 10},
        "filter": {},
    })
    assert auto["callId"].startswith("local-")
    ok(auto)
    prefixed = ok(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"callId": "call-q1", "field": "result.total",
                   "value": total},
        "why": "leading result stripped", "rationale": "probe",
    }, session))
    assert prefixed["id"] == "E1"
    listed = ok(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"callId": "call-q1", "field": "entries.0.insertId",
                   "value": session.calls["call-q1"].result[
                       "entries"][0]["insertId"]},
        "why": "numeric index", "rationale": "probe",
    }, session))
    assert listed["id"] == "E2"
    logged = ok(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": good_id},
        "why": "good log", "rationale": "probe",
    }, session))
    assert logged["id"] == "E3"
    stored = session.evidence["E3"]
    assert stored.deployment == deployment
    assert isinstance(stored.value, dict)
    assert stored.value["insertId"] == good_id
    refused(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": past[-2].insertId},
        "why": "fourth", "rationale": "probe",
    }, session), "evidence.limit")
    saved_now = setup["clock"].now()
    setup["world"].step_until(
        saved_now + timedelta(seconds=600)
    )
    future = [
        e for e in entries_for(setup, env) if e.timestamp > saved_now
    ]
    assert future
    setup["clock"].set(saved_now)
    refused(tools.call("record_evidence", {
        "hypothesis": "H2", "stance": "refutes",
        "source": {"insertId": future[-1].insertId},
        "why": "future entry", "rationale": "probe",
    }, session), "evidence.future")


def _diagnosis_base(incident: object, contract: object) -> dict:
    tripwire = next(
        t for t in contract.tripwires if t.id == incident.tripwire
    )
    return {
        "incidentId": incident.id,
        "deployment": incident.deployment,
        "component": tripwire.component,
        "category": "connector_stalled",
        "hypothesis": "H1",
        "summary": "connector stopped running",
        "evidence": ["E1"],
        "openQuestions": [{"hypothesis": "H2", "why": "open"}],
        "action": {"type": "none"},
        "expectedVerification": {
            "tripwire": incident.tripwire,
            "condition": "recovers_within", "seconds": 300,
        },
        "confidence": "medium",
    }


def _submit(tools: object, session: object, diag: dict) -> dict:
    return tools.call("submit_diagnosis", {
        "diagnosis": diag, "rationale": "t7 probe",
    }, session)


def test_d5_t7_diagnosis_gate():
    """D5-T7: every §6.5 rule plus category rows refuse naming the rule."""
    from fleetsre.evaluator import tripwires as tripmod

    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    incident = build_incident(setup, registry)
    session = _session(incident.id)
    _set_ok(tools, session)
    env = incident.deployment.split("/", 1)[1]
    own = entries_for(setup, env)
    now = setup["clock"].now()
    past = [e for e in own if e.timestamp <= now]
    hb_event = tripwire_of(
        incident.deployment.split("/", 1)[0], incident.tripwire
    ).spec["event"]
    heartbeats = [
        e for e in past if e.jsonPayload.get("event") == hb_event
    ]
    assert heartbeats
    latest_hb = max(heartbeats, key=lambda e: e.timestamp).insertId
    ok(_read(tools, session, "query_logs", {
        "deployment": incident.deployment,
        "window": {"lastMinutes": 120}, "filter": {},
    }))
    ok(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": latest_hb},
        "why": "latest heartbeat", "rationale": "t7",
    }, session))

    base = _diagnosis_base(incident, setup["contract"])
    ok(_submit(tools, session, copy.deepcopy(base)))
    bad_schema = copy.deepcopy(base)
    del bad_schema["category"]
    refused(_submit(tools, session, bad_schema), "diagnosis.schema")
    unrecorded = copy.deepcopy(base)
    unrecorded["evidence"] = ["E99"]
    refused(_submit(tools, session, unrecorded), "diagnosis.evidence")
    nocover = copy.deepcopy(base)
    nocover["openQuestions"] = []
    refused(_submit(tools, session, nocover), "diagnosis.coverage")
    bad_action = copy.deepcopy(base)
    bad_action["action"] = {"type": "propose",
                            "method": "connectorInstances.delete",
                            "resource": "x", "updateMask": [],
                            "patch": {}}
    refused(_submit(tools, session, bad_action), "diagnosis.action")
    flt = setup["fleet"]
    conn = connector_name(flt, env)
    bad_mask = copy.deepcopy(base)
    bad_mask["action"] = {"type": "propose",
                          "method": "connectorInstances.patch",
                          "resource": conn,
                          "updateMask": ["parameters"],
                          "patch": {"enabled": False}}
    refused(_submit(tools, session, bad_mask), "diagnosis.update_mask")
    bad_mask_field = copy.deepcopy(base)
    bad_mask_field["action"] = {
        "type": "propose", "method": "connectorInstances.patch",
        "resource": conn, "updateMask": ["displayName"],
        "patch": {"displayName": "x"},
    }
    refused(_submit(tools, session, bad_mask_field),
            "diagnosis.update_mask")
    other_env = sibling_of(setup, incident.deployment).split("/", 1)[1]
    foreign = connector_name(flt, other_env)
    bad_resource = copy.deepcopy(base)
    bad_resource["action"] = {"type": "propose",
                              "method": "connectorInstances.restart",
                              "resource": foreign,
                              "updateMask": [], "patch": {}}
    refused(_submit(tools, session, bad_resource), "diagnosis.resource")
    bad_inc = copy.deepcopy(base)
    bad_inc["incidentId"] = "inc-other"
    refused(_submit(tools, session, bad_inc), "diagnosis.incident")
    bad_dep = copy.deepcopy(base)
    bad_dep["deployment"] = sibling_of(setup, incident.deployment)
    refused(_submit(tools, session, bad_dep), "diagnosis.incident")
    bad_comp = copy.deepcopy(base)
    bad_comp["component"] = "nope"
    refused(_submit(tools, session, bad_comp), "diagnosis.component")
    bad_hyp = copy.deepcopy(base)
    bad_hyp["hypothesis"] = "H9"
    refused(_submit(tools, session, bad_hyp), "diagnosis.hypothesis")
    no_verify = copy.deepcopy(base)
    del no_verify["expectedVerification"]
    refused(_submit(tools, session, no_verify), "diagnosis.verification")
    foreign_tripwire = copy.deepcopy(base)
    foreign_tripwire["expectedVerification"] = {
        "tripwire": "dead-job", "condition": "recovers_within",
        "seconds": 300,
    }
    refused(_submit(tools, session, foreign_tripwire),
            "diagnosis.verification")
    empty_esc = copy.deepcopy(base)
    empty_esc["category"] = "unknown"
    empty_esc["action"] = {"type": "escalate", "escalationSummary": ""}
    refused(_submit(tools, session, empty_esc), "diagnosis.action")
    stale_none = copy.deepcopy(base)
    ok(_submit(tools, session, stale_none))
    session.turn = 6
    session.resumed_at_turns = [5]
    refused(_submit(tools, session, copy.deepcopy(base)),
            "diagnosis.none_after_resume")
    fresh = ok(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": past[-3].insertId},
        "why": "after resume", "rationale": "t7",
    }, session))
    assert session.evidence[fresh["id"]].recordedAtTurn == 6
    after_resume = copy.deepcopy(base)
    after_resume["evidence"] = [fresh["id"]]
    ok(_submit(tools, session, after_resume))
    bad_unknown = copy.deepcopy(base)
    bad_unknown["category"] = "unknown"
    bad_unknown["action"] = {"type": "propose",
                             "method": "connectorInstances.restart",
                             "resource": conn, "updateMask": [],
                             "patch": {}}
    refused(_submit(tools, session, bad_unknown), "diagnosis.category")

    cat_cases = [
        ("F1", "connector_stalled"),
        ("F7", "job_stalled"),
        ("F2", "credential_failure"),
        ("F3", "schema_drift"),
        ("F4", "job_hollow_success"),
        ("F5", "job_failing"),
    ]
    for fault, category in cat_cases:
        case = setup_from_fixture(fault)
        if fault == "F4":
            f4_env = case["trip"].deployment.split("/", 1)[1]
            hollow_src = next(
                e for e in entries_for(case, f4_env)
                if e.jsonPayload.get("event") == "job.run.completed"
                and e.jsonPayload.get("status") == "SUCCESS"
                and e.jsonPayload.get("indicatorsSynced") == 0
            )
            payload = copy.deepcopy(hollow_src.jsonPayload)
            payload["status"] = "ERROR"
            case["sink"].write(hollow_src.model_copy(update={
                "insertId": f"{hollow_src.insertId}-nm",
                "jsonPayload": payload,
            }))
        ctools, creg, _p = build_tools(case)
        cinc = build_incident(case, creg)
        csess = _session(cinc.id)
        _set_ok(ctools, csess)
        ok(_read(ctools, csess, "query_logs", {
            "deployment": cinc.deployment,
            "window": {"lastMinutes": 120}, "filter": {},
        }))
        cenv = cinc.deployment.split("/", 1)[1]
        cown = [e for e in entries_for(case, cenv)
                if e.timestamp <= case["clock"].now()]
        tool_ids: list[str] = []
        if category in ("connector_stalled", "job_stalled"):
            event = tripwire_of(
                cinc.deployment.split("/", 1)[0], cinc.tripwire
            ).spec["event"]
            cands = sorted(
                (e for e in cown
                 if e.jsonPayload.get("event") == event),
                key=lambda e: e.timestamp,
            )
            assert len(cands) >= 2
            cite = cands[-1].insertId
            near = cands[-2].insertId
        elif category == "credential_failure":
            cands = [e for e in cown if e.severity in ("WARNING", "ERROR")]
            cite = max(cands, key=lambda e: e.timestamp).insertId
            integ = sentinel_integration_name(case["fleet"], cenv)
            ok(_read(ctools, csess, "test_integration_instance",
                      {"name": integ}))
            tcall = next(
                cid for cid, c in csess.calls.items()
                if c.tool == "test_integration_instance"
            )
            assert csess.calls[tcall].result["successful"] is False
            tool_ev = ok(ctools.call("record_evidence", {
                "hypothesis": "H1", "stance": "supports",
                "source": {"callId": tcall, "field": "successful",
                           "value": False},
                "why": "test failed", "rationale": "t7",
            }, csess))
            tool_ids.append(tool_ev["id"])
            near = ""
        elif category == "schema_drift":
            from fleetsre.contracts import SchemaSpec

            spec = tripwire_of(
                cinc.deployment.split("/", 1)[0], cinc.tripwire
            ).spec
            schema = SchemaSpec(
                event=spec.get("event"), field=spec["field"],
                type=spec["type"], enum=spec.get("enum"),
                minimum=spec.get("minimum"),
                required=bool(spec["required"]),
                consecutive=int(spec.get("consecutive", 1)),
                recoverAfter=spec.get("recoverAfter"),
                where=spec.get("where"),
            )
            bad = [e for e in cown
                   if tripmod.schema_violation(e, schema) is not None]
            assert bad
            cite = bad[-1].insertId
            valid_entries = [
                e for e in cown
                if e.jsonPayload.get("event") == "alert.ingested"
                and tripmod.schema_violation(e, schema) is None
            ]
            assert valid_entries
            near = valid_entries[-1].insertId
        elif category == "job_hollow_success":
            cands = [e for e in cown
                     if e.jsonPayload.get("event") == "job.run.completed"
                     and e.jsonPayload.get("status") == "SUCCESS"
                     and e.jsonPayload.get("indicatorsSynced") == 0]
            assert cands
            cite = cands[-1].insertId
            near = f"{cands[-1].insertId}-nm"
            assert near in {e.insertId for e in cown}
        else:
            cands = [e for e in cown if e.severity == "ERROR"
                     and e.jsonPayload.get("event") in (
                         "job.run.error", "job.run.completed")]
            assert cands
            cite = cands[-1].insertId
            good_runs = [
                e for e in cown
                if e.jsonPayload.get("event") == "job.run.completed"
                and e.jsonPayload.get("status") == "SUCCESS"
            ]
            assert good_runs
            near = good_runs[-1].insertId
        rec = ok(ctools.call("record_evidence", {
            "hypothesis": "H1", "stance": "supports",
            "source": {"insertId": cite},
            "why": "category signal", "rationale": "t7",
        }, csess))
        diag = _diagnosis_base(cinc, case["contract"])
        diag["category"] = category
        diag["evidence"] = [rec["id"], *tool_ids]
        ok(_submit(ctools, csess, copy.deepcopy(diag)))
        if category == "credential_failure":
            refused(_submit(ctools, csess, {**diag, "evidence": [rec["id"]]}),
                    "diagnosis.category")
            refused(_submit(ctools, csess, {**diag, "evidence": tool_ids}),
                    "diagnosis.category")
        else:
            wrong = ok(ctools.call("record_evidence", {
                "hypothesis": "H1", "stance": "supports",
                "source": {"insertId": near},
                "why": "near miss", "rationale": "t7",
            }, csess))
            diag_bad = copy.deepcopy(diag)
            diag_bad["evidence"] = [wrong["id"]]
            refused(_submit(ctools, csess, diag_bad),
                    "diagnosis.category")


def test_d5_t8_refusal_cap():
    """D5-T8: three refused submissions pause; refusals are transcribed."""
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    incident = build_incident(setup, registry)
    store = MemorySessionStore()
    turns = [scripted_response(1, "set_hypotheses", {
        "items": valid_hypotheses(), "rationale": "t8",
    })]
    for turn in (2, 3, 4):
        turns.append(scripted_response(turn, "submit_diagnosis", {
            "diagnosis": {"incidentId": incident.id},
            "rationale": "bad submit",
        }))
    transport = ScriptedTransport(turns)
    gateway = ModelGateway(gateway_config(), transport)
    result = investigate(
        incident, tools, gateway, store, max_turns=12,
        max_read_calls=12, max_tokens=400_000,
    )
    assert result.status == "needs_input"
    assert result.reason == "refused"
    assert result.diagnosis is None
    transcript = store.transcript(result.transcript_id)
    refused_turns = [r for r in transcript if r.get("verdict") == "refused"
                     and r.get("tool") == "submit_diagnosis"]
    assert len(refused_turns) == 3
    assert all(r.get("problems") for r in refused_turns)


def test_d5_t9_budgets():
    """D5-T9: last-turn warning, read cap refuses, token cap pauses."""
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    incident = build_incident(setup, registry)
    deployment = incident.deployment
    store = MemorySessionStore()
    plans = [scripted_response(t, "update_plan", {
        "plan": [], "rationale": f"turn {t}",
    }) for t in (1, 2, 3)]
    transport = ScriptedTransport(plans)
    gateway = ModelGateway(gateway_config(), transport)
    result = investigate(
        incident, tools, gateway, store, max_turns=3,
        max_read_calls=12, max_tokens=400_000,
    )
    assert result.status == "needs_input"
    assert result.reason == "budget"
    last_request = transport.requests[-1]
    assert last_request["input"][-1] == {
        "role": "user",
        "content": ("This is your last turn. Call submit_diagnosis "
                    "now with what you have."),
    }

    store2 = MemorySessionStore()
    reads = [scripted_response(1, "set_hypotheses", {
        "items": valid_hypotheses(), "rationale": "t9",
    })]
    for turn, mins in ((2, 10), (3, 20), (4, 30)):
        reads.append(scripted_response(turn, "query_logs", {
            "deployment": deployment,
            "window": {"lastMinutes": mins}, "filter": {},
            "rationale": f"read {turn}",
        }))
    reads.append(scripted_response(5, "update_plan", {
        "plan": [], "rationale": "after cap",
    }))
    transport2 = ScriptedTransport(reads)
    gateway2 = ModelGateway(gateway_config(), transport2)
    result2 = investigate(
        incident, tools, gateway2, store2, max_turns=5,
        max_read_calls=2, max_tokens=400_000,
    )
    assert result2.status == "needs_input"
    assert result2.reason == "budget"
    assert result2.turns == 5
    transcript2 = store2.transcript(result2.transcript_id)
    capped = [r for r in transcript2
              if r.get("tool") == "query_logs"
              and r.get("verdict") == "refused"]
    assert len(capped) >= 1
    assert any(
        p.startswith("reads.cap:")
        for r in capped for p in r.get("problems", [])
    )

    store3 = MemorySessionStore()
    hungry = [scripted_response(
        t, "update_plan", {"plan": [], "rationale": "hungry"},
        input_tokens=600, output_tokens=600,
    ) for t in (1, 2)]
    transport3 = ScriptedTransport(hungry)
    gateway3 = ModelGateway(gateway_config(), transport3)
    result3 = investigate(
        incident, tools, gateway3, store3, max_turns=12,
        max_read_calls=12, max_tokens=1000,
    )
    assert result3.status == "needs_input"
    assert result3.reason == "token_budget"


def test_d5_t10_stuck():
    """D5-T10: the same call a third time pauses before running it."""
    from fastapi.testclient import TestClient

    from fleetsre.adapters.chronicle_http import ChronicleHttpClient
    from fleetsre.mock.chronicle_api import create_app
    from fleetsre.ports import MemoryLogQuery, MemoryStateStore
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore
    from fleetsre.sre.tools import ToolSet

    setup = setup_from_fixture("F1")
    inner = MemoryLogQuery(setup["sink"])
    counting = CountingLogQuery(inner)
    http = TestClient(create_app(setup["world"], dev_auth=True))
    chronicle = ChronicleHttpClient(http, "obs-reader")
    state = MemoryStateStore()
    registry = build_registry([setup["contract"]], setup["deployments"])
    tools = ToolSet(
        counting, chronicle, state, registry,
        clock=setup["clock"], verification=True,
    )
    incident = build_incident(setup, registry)
    store = MemorySessionStore()
    deployment = incident.deployment
    args_a = {"deployment": deployment,
              "window": {"lastMinutes": 10}, "filter": {},
              "rationale": "first wording"}
    args_b = {"filter": {}, "window": {"lastMinutes": 10},
              "deployment": deployment,
              "rationale": "second wording"}
    args_c = {"deployment": deployment, "filter": {},
              "window": {"lastMinutes": 10},
              "rationale": "third wording"}
    turns = [
        {"output": [reasoning(1),
                    fn_call("query_logs", args_a, "call_001")],
         "usage": usage(1)},
        {"output": [reasoning(2),
                    fn_call("query_logs", args_b, "call_002")],
         "usage": usage(2)},
        {"output": [reasoning(3),
                    fn_call("query_logs", args_c, "call_003")],
         "usage": usage(3)},
    ]
    transport = ScriptedTransport(turns)
    gateway = ModelGateway(gateway_config(), transport)
    result = investigate(
        incident, tools, gateway, store, max_turns=12,
        max_read_calls=12, max_tokens=400_000,
    )
    assert result.status == "needs_input"
    assert result.reason == "stuck"
    assert counting.entries_calls == 2


def test_d5_t11_import_boundary():
    """D5-T11: sre keeps off google/adapters/mock; lint-imports passes."""
    text = (ROOT / ".importlinter").read_text()
    assert "fleetsre.sre" in text
    for forbidden in ("google", "fleetsre.adapters", "fleetsre.mock"):
        assert forbidden in text, forbidden
    assert "exclude_type_checking_imports" in text
    proc = subprocess.run(
        [sys.executable, "-m", "lint_imports"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        proc = subprocess.run(
            ["lint-imports"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SRE isolation KEPT" in (proc.stdout + proc.stderr)
    assert "Tripwire purity KEPT" in (proc.stdout + proc.stderr)


def test_d5_t12_transcript_and_per_turn_save():
    """D5-T12: per-turn transcript with tokens/cost; crash resumes at k+1."""
    from fastapi.testclient import TestClient

    from fleetsre.adapters.chronicle_http import ChronicleHttpClient
    from fleetsre.mock.chronicle_api import create_app
    from fleetsre.ports import MemoryLogQuery, MemoryStateStore
    from fleetsre.sre.gateway import ModelGateway
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore
    from fleetsre.sre.tools import ToolSet

    setup = setup_from_fixture("F1")
    inner = MemoryLogQuery(setup["sink"])
    counting = CountingLogQuery(inner)
    http = TestClient(create_app(setup["world"], dev_auth=True))
    chronicle = ChronicleHttpClient(http, "obs-reader")
    state = MemoryStateStore()
    registry = build_registry([setup["contract"]], setup["deployments"])
    tools = ToolSet(
        counting, chronicle, state, registry,
        clock=setup["clock"], verification=True,
    )
    incident = build_incident(setup, registry)
    store = MemorySessionStore()
    deployment = incident.deployment

    class CrashAfter:
        def __init__(self, responses: list, crash_at: int) -> None:
            self.responses = responses
            self.crash_at = crash_at
            self.requests: list = []

        def create(self, request: dict) -> dict:
            self.requests.append(request)
            if len(self.requests) == self.crash_at:
                raise RuntimeError("simulated crash")
            return self.responses[len(self.requests) - 1]

    first_two = [
        scripted_response(1, "set_hypotheses", {
            "items": valid_hypotheses(), "rationale": "t12",
        }),
        scripted_response(2, "query_logs", {
            "deployment": deployment,
            "window": {"lastMinutes": 10}, "filter": {},
            "rationale": "t12 read",
        }),
    ]
    crasher = CrashAfter(first_two, 3)
    gateway = ModelGateway(gateway_config(), crasher)
    with pytest.raises(RuntimeError, match="simulated crash"):
        investigate(
            incident, tools, gateway, store, max_turns=12,
            max_read_calls=12, max_tokens=400_000,
        )
    calls_before = counting.entries_calls
    assert calls_before == 1
    saved = store.load(incident.id)
    assert saved.turn == 2
    assert saved.status == "running"
    rest = CrashAfter([scripted_response(3, "update_plan", {
        "plan": [], "rationale": "after crash",
    })], 2)
    gateway2 = ModelGateway(gateway_config(), rest)
    with pytest.raises(RuntimeError, match="simulated crash"):
        investigate(
            incident, tools, gateway2, store, max_turns=12,
            max_read_calls=12, max_tokens=400_000,
        )
    assert counting.entries_calls == calls_before
    assert rest.requests[0]["input"] == crasher.requests[2]["input"]
    transcript = store.transcript(saved.transcript_id)
    assert len(transcript) >= 2
    for record in transcript:
        assert "output" in record or "tool" in record
    token_records = [r for r in transcript if "usage" in r]
    assert token_records
    for record in token_records:
        use = record["usage"]
        assert "input_tokens" in use and "output_tokens" in use
        assert "cached_tokens" in use and "reasoning_tokens" in use
        assert "cost_usd" in record


def test_d5_t13_resume(tmp_path):
    """D5-T13: resume keeps session, extends budget, turns keep counting."""
    from fleetsre.adapters.sqlite_state import SqliteSessionStore
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import investigate, resume

    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    incident = build_incident(setup, registry)
    store = SqliteSessionStore(tmp_path / "t13.sqlite")
    deployment = incident.deployment
    env = deployment.split("/", 1)[1]
    own = [e for e in entries_for(setup, env)
           if e.timestamp <= setup["clock"].now()]
    id1, id2 = own[-1].insertId, own[-2].insertId
    message = "operator granted more turns"
    first = [
        scripted_response(1, "set_hypotheses", {
            "items": valid_hypotheses(), "rationale": "t13",
        }),
        scripted_response(2, "query_logs", {
            "deployment": deployment,
            "window": {"lastMinutes": 10}, "filter": {},
            "rationale": "t13 read",
        }),
        scripted_response(3, "record_evidence", {
            "hypothesis": "H1", "stance": "supports",
            "source": {"insertId": id1},
            "why": "segment one item", "rationale": "t13",
        }),
    ]
    transport = ScriptedTransport(first)
    gateway = ModelGateway(gateway_config(), transport)
    paused = investigate(
        incident, tools, gateway, store, max_turns=3,
        max_read_calls=12, max_tokens=400_000,
    )
    assert paused.status == "needs_input"
    assert paused.reason == "budget"
    before = store.load(incident.id)
    assert set(before.hypotheses) == {"H1", "H2"}
    assert before.evidence["E1"].recordedAtTurn == 3
    follow = [
        scripted_response(4, "record_evidence", {
            "hypothesis": "H1", "stance": "supports",
            "source": {"insertId": id2},
            "why": "segment two item", "rationale": "t13",
        }),
        scripted_response(5, "update_plan", {
            "plan": [], "rationale": "resumed turn",
        }),
    ]
    transport2 = ScriptedTransport(follow)
    gateway2 = ModelGateway(gateway_config(), transport2)
    resumed = resume(
        incident, tools, gateway2, store,
        message=message, extra_turns=2,
    )
    assert resumed.reason == "budget"
    assert transport2.requests
    assert transport2.requests[0]["input"][-1] == {
        "role": "user", "content": message,
    }
    after = store.load(incident.id)
    assert set(after.hypotheses) == {"H1", "H2"}
    assert after.evidence["E1"].recordedAtTurn == 3
    assert after.evidence["E2"].recordedAtTurn == 4
    assert after.resumed_at_turns == [3]
    assert after.segment_start_turn == 3
    assert after.turn_limit == 5
    assert resumed.transcript_id == paused.transcript_id
    fresh = SqliteSessionStore(tmp_path / "t13.sqlite")
    reloaded = fresh.load(incident.id)
    assert reloaded.evidence["E1"].recordedAtTurn == 3
    assert reloaded.evidence["E2"].recordedAtTurn == 4


def test_d5_t14_backoff():
    """D5-T14: 429/5xx back off 1,2,4..; retries out means unavailable."""
    from fleetsre.sre.gateway import (
        ModelGateway,
        ScriptedTransport,
        TransportError,
    )
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    incident = build_incident(setup, registry)
    slept: list[float] = []
    ok_response = scripted_response(1, "update_plan", {
        "plan": [], "rationale": "t14",
    })
    flaky = ScriptedTransport([
        {"_error": 429}, {"_error": 429}, {"_error": 503}, ok_response,
    ])
    gateway = ModelGateway(
        gateway_config(max_retries=5), flaky, sleep=slept.append
    )
    out = gateway.respond("investigate", [{"role": "user"}], None)
    assert out.output
    assert slept == [1, 2, 4]
    tired = ScriptedTransport([{"_error": 429}] * 6)
    slept2: list[float] = []
    gateway2 = ModelGateway(
        gateway_config(max_retries=2), tired, sleep=slept2.append
    )
    store2 = MemorySessionStore()
    result = investigate(
        incident, tools, gateway2, store2, max_turns=4,
        max_read_calls=12, max_tokens=400_000,
    )
    assert result.status == "needs_input"
    assert result.reason == "model_unavailable"
    assert slept2 == [1, 2]
    fatal = ScriptedTransport([{"_error": 400}])
    slept3: list[float] = []
    gateway3 = ModelGateway(
        gateway_config(max_retries=5), fatal, sleep=slept3.append
    )
    with pytest.raises(TransportError):
        gateway3.respond("investigate", [{"role": "user"}], None)
    assert slept3 == []


def test_d5_t15_role_config():
    """D5-T15: role model/effort flow into requests with no code change."""
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    incident = build_incident(setup, registry)
    for model, effort in (("test-model-a", "low"),
                          ("test-model-b", "high")):
        store = MemorySessionStore()
        transport = ScriptedTransport([scripted_response(1, "update_plan", {
            "plan": [], "rationale": "t15",
        })])
        gateway = ModelGateway(
            gateway_config(model=model, effort=effort), transport
        )
        investigate(
            incident, tools, gateway, store, max_turns=1,
            max_read_calls=12, max_tokens=400_000,
        )
        assert transport.requests
        assert transport.requests[0]["model"] == model
        assert transport.requests[0]["reasoning"] == {"effort": effort}


def test_d5_t16_scripted_end_to_end():
    """D5-T16: scripted F1-F5/F7 submit the mocks §6 right answers."""
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    for fault in FAULTS:
        setup = setup_from_fixture(fault)
        tools, registry, _parts = build_tools(setup)
        incident = build_incident(setup, registry)
        store = MemorySessionStore()
        fixture = setup["fixture"]
        assert incident.id == fixture["incident"]["id"], fault
        transport = ScriptedTransport(fixture["responses"])
        gateway = ModelGateway(gateway_config(), transport)
        result = investigate(
            incident, tools, gateway, store, max_turns=24,
            max_read_calls=12, max_tokens=400_000,
        )
        want_category, want_action = RIGHT[fault]
        service = incident.deployment.split("/", 1)[0]
        want_component = tripwire_of(service, incident.tripwire).component
        assert result.diagnosis is not None, fault
        assert result.diagnosis["category"] == want_category, fault
        assert result.diagnosis["action"]["type"] == want_action, fault
        assert result.diagnosis["deployment"] == incident.deployment, fault
        assert result.diagnosis["component"] == want_component, fault
        if want_action == "escalate":
            assert result.status == "needs_input", fault
            assert result.reason == "escalated", fault
        else:
            assert result.status == "submitted", fault
            assert result.reason == "propose", fault
        transcript = store.transcript(result.transcript_id)
        refused_turns = [r for r in transcript
                         if r.get("verdict") == "refused"]
        assert refused_turns == [], fault


def test_d5_t17_recorded_replay():
    """D5-T17: recordings replay identically; ciphertext stays stripped."""
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    recorded_dir = ROOT / "fixtures" / "recorded"
    paths = sorted(recorded_dir.glob("*.json")) if recorded_dir.is_dir() \
        else []
    if not paths:
        raise FileNotFoundError(
            f"no recordings yet in {recorded_dir}"
        )
    for path in paths:
        text = path.read_text()
        assert "enc-placeholder-" not in text, path.name
        data = json.loads(text)
        for response in data.get("responses", []):
            for item in response.get("output", []):
                if "encrypted_content" in item:
                    assert item["encrypted_content"] == "<stripped>", (
                        path.name, item.get("id")
                    )

    def _key(record: dict) -> tuple:
        return (
            record.get("tool"),
            json.dumps(record.get("args", {}), sort_keys=True),
            record.get("verdict"),
            tuple(sorted(record.get("problems", []))),
            json.dumps(record.get("output", {}), sort_keys=True),
        )

    for path in paths:
        data = json.loads(path.read_text())
        deployment = data["incident"]["deployment"]
        service = deployment.split("/", 1)[0]
        setup = trip_until(
            data["seed"], data["faults"], service, deployment,
            data["incident"]["tripwire"],
        )
        tools, registry, _parts = build_tools(setup)
        incident = build_incident(setup, registry)
        store = MemorySessionStore()
        transport = ScriptedTransport(data["responses"])
        gateway = ModelGateway(gateway_config(), transport)
        result = investigate(
            incident, tools, gateway, store, max_turns=24,
            max_read_calls=12, max_tokens=400_000,
        )
        transcript = store.transcript(result.transcript_id)
        actual = [_key(r) for r in transcript if r.get("tool")]
        expected = [_key(r) for r in data["transcript"] if r.get("tool")]
        assert actual == expected, path.name
        assert (result.diagnosis or {}) == (data.get("diagnosis") or {}), (
            path.name
        )


def test_d5_t18_gates_off():
    """D5-T18: verification=False records wouldRefuse; schema still bars."""
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup, verification=False)
    incident = build_incident(setup, registry)
    session = _session(incident.id)
    ok(tools.call("set_hypotheses", {
        "items": [{"id": "H1", "statement": "s", "wouldConfirm": "c",
                   "alternative": False}],
        "rationale": "gates off",
    }, session))
    ok(tools.call("record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": "insert-missing-000"},
        "why": "gates off", "rationale": "gates off",
    }, session))
    refused(tools.call("submit_diagnosis", {
        "diagnosis": {"incidentId": incident.id},
        "rationale": "schema still refuses",
    }, session), "diagnosis.schema")
    breaking = _diagnosis_base(incident, setup["contract"])
    breaking["evidence"] = ["E99"]
    ok(tools.call("submit_diagnosis", {
        "diagnosis": breaking, "rationale": "gates off",
    }, session))
    store = MemorySessionStore()
    turns = [
        scripted_response(1, "set_hypotheses", {
            "items": [{"id": "H1", "statement": "s",
                       "wouldConfirm": "c", "alternative": False}],
            "rationale": "would refuse",
        }),
        scripted_response(2, "record_evidence", {
            "hypothesis": "H1", "stance": "supports",
            "source": {"insertId": "insert-missing-000"},
            "why": "would refuse", "rationale": "would refuse",
        }),
        scripted_response(3, "submit_diagnosis", {
            "diagnosis": breaking, "rationale": "would refuse",
        }),
    ]
    transport = ScriptedTransport(turns)
    gateway = ModelGateway(gateway_config(), transport)
    result = investigate(
        incident, tools, gateway, store, max_turns=3,
        max_read_calls=12, max_tokens=400_000,
    )
    transcript = store.transcript(result.transcript_id)
    would = [r for r in transcript if r.get("verdict") == "wouldRefuse"]
    assert len(would) >= 3
    would_submit = [r for r in would
                    if r.get("tool") == "submit_diagnosis"]
    assert would_submit
    assert any(
        p.startswith("diagnosis.evidence:")
        for p in would_submit[0].get("problems", [])
    )
    capped = ScriptedTransport([
        scripted_response(1, "set_hypotheses", {
            "items": valid_hypotheses(), "rationale": "t18",
        }),
        scripted_response(2, "query_logs", {
            "deployment": incident.deployment,
            "window": {"lastMinutes": 10}, "filter": {},
            "rationale": "read one",
        }),
        scripted_response(3, "query_logs", {
            "deployment": incident.deployment,
            "window": {"lastMinutes": 20}, "filter": {},
            "rationale": "read two",
        }),
    ])
    store2 = MemorySessionStore()
    gateway2 = ModelGateway(gateway_config(), capped)
    result2 = investigate(
        incident, tools, gateway2, store2, max_turns=3,
        max_read_calls=1, max_tokens=400_000,
    )
    transcript2 = store2.transcript(result2.transcript_id)
    capped_reads = [r for r in transcript2
                    if r.get("tool") == "query_logs"
                    and r.get("verdict") == "refused"]
    assert len(capped_reads) >= 1
    assert any(
        p.startswith("reads.cap:")
        for r in capped_reads for p in r.get("problems", [])
    )


@pytest.mark.live
def test_d5_t19_live_eval():
    """D5-T19: live 3 runs per fault hit the right answers within budget."""
    import os

    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    date = datetime.now(timezone.utc).date().isoformat()
    eval_path = ROOT / "evals" / f"d5-{date}.json"
    eval_path.parent.mkdir(parents=True, exist_ok=True)
    live_config, live_model, prices_zero = live_gateway_config()
    results: dict = {
        "date": date, "model": live_model,
        "prices": ("zero (FLEETSRE_PRICES_JSON not set)"
                   if prices_zero else "FLEETSRE_PRICES_JSON"),
        "faults": {},
    }
    for fault in FAULTS:
        setup = setup_from_fixture(fault)
        tools, registry, _parts = build_tools(setup)
        incident = build_incident(setup, registry)
        if not os.environ.get("MODEL_API_KEY"):
            pytest.skip("MODEL_API_KEY is not set; live eval needs a key")
        from fleetsre.sre.gateway import ModelGateway, OpenAITransport

        want_category, want_action = RIGHT[fault]
        service = incident.deployment.split("/", 1)[0]
        want_component = tripwire_of(service, incident.tripwire).component
        runs = []
        for run in range(3):
            store = MemorySessionStore()
            transport = OpenAITransport(
                api_key=os.environ["MODEL_API_KEY"],
                base_url="https://api.meta.ai/v1",
            )
            gateway = ModelGateway(live_config, transport)
            result = investigate(
                incident, tools, gateway, store, max_turns=24,
                max_read_calls=12, max_tokens=400_000,
            )
            diag = result.diagnosis or {}
            transcript = store.transcript(result.transcript_id)
            refusals: dict = {}
            for record in transcript:
                if record.get("verdict") == "refused":
                    gate = record.get("tool", "unknown")
                    refusals[gate] = refusals.get(gate, 0) + 1
            called = sorted({
                r.get("tool") for r in transcript if r.get("tool")
            })
            tokens = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
            cost_usd = 0.0
            for record in transcript:
                use = record.get("usage")
                if not isinstance(use, dict):
                    continue
                tokens["input"] += int(use.get("input_tokens", 0))
                tokens["cached"] += int(use.get("cached_tokens", 0))
                tokens["output"] += int(use.get("output_tokens", 0))
                tokens["reasoning"] += int(use.get("reasoning_tokens", 0))
                cost_usd += float(record.get("cost_usd", 0.0))
            good = (
                diag.get("deployment") == incident.deployment
                and diag.get("component") == want_component
                and diag.get("category") == want_category
                and result.turns <= 24
            )
            if want_action == "escalate":
                good = good and result.status == "needs_input" \
                    and result.reason == "escalated"
            else:
                good = good and result.status == "submitted"
            runs.append({
                "run": run, "status": result.status,
                "reason": result.reason, "turns": result.turns,
                "read_calls": result.read_calls,
                "tokens": tokens, "cost_usd": cost_usd,
                "refusals": refusals,
                "tools_called": called, "diagnosis": diag,
                "accepted": bool(good),
            })
        results["faults"][fault] = runs
    results["total_cost_usd"] = sum(
        r["cost_usd"] for runs in results["faults"].values() for r in runs
    )
    eval_path.write_text(json.dumps(results, indent=2) + "\n")
    for fault in FAULTS:
        runs = results["faults"][fault]
        assert sum(1 for r in runs if r["accepted"]) >= 2, fault


@pytest.mark.live
def test_d5_t20_reported_not_gating():
    """D5-T20: eval file reports refusals, F2 tools and gates-off counts."""
    import os

    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.session import MemorySessionStore

    date = datetime.now(timezone.utc).date().isoformat()
    eval_path = ROOT / "evals" / f"d5-{date}.json"
    setup = setup_from_fixture("F1")
    tools, registry, _parts = build_tools(setup)
    _incident = build_incident(setup, registry)
    if not os.environ.get("MODEL_API_KEY"):
        pytest.skip("MODEL_API_KEY is not set; live eval needs a key")
    if not eval_path.is_file():
        raise FileNotFoundError(f"run D5-T19 first: {eval_path}")
    from fleetsre.sre.gateway import ModelGateway, OpenAITransport

    data = json.loads(eval_path.read_text())
    assert "faults" in data
    live_config, _, _ = live_gateway_config()
    for fault in FAULTS:
        assert fault in data["faults"], fault
        for run in data["faults"][fault]:
            assert "refusals" in run, (fault, run)
    f2_runs = data["faults"]["F2"]
    probing = sum(
        1 for r in f2_runs
        if "compare_siblings" in r.get("tools_called", [])
        or "test_integration_instance" in r.get("tools_called", [])
    )
    data["f2_probe_share"] = probing / max(len(f2_runs), 1)
    off_counts: dict = {}
    for fault in FAULTS:
        case = setup_from_fixture(fault)
        ctools, creg, _p = build_tools(case, verification=False)
        cinc = build_incident(case, creg)
        store = MemorySessionStore()
        transport = OpenAITransport(
            api_key=os.environ["MODEL_API_KEY"],
            base_url="https://api.meta.ai/v1",
        )
        gateway = ModelGateway(live_config, transport)
        result = investigate(
            cinc, ctools, gateway, store, max_turns=24,
            max_read_calls=12, max_tokens=400_000,
        )
        transcript = store.transcript(result.transcript_id)
        by_gate: dict = {}
        for record in transcript:
            if record.get("verdict") == "wouldRefuse":
                gate = record.get("tool", "unknown")
                by_gate[gate] = by_gate.get(gate, 0) + 1
        off_counts[fault] = by_gate
    data["gates_off_would_refuse"] = off_counts
    eval_path.write_text(json.dumps(data, indent=2) + "\n")
