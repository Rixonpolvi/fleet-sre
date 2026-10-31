#!/usr/bin/env python3
"""Generate fixtures/scripted/{F1,F2,F3,F4,F5,F7}.json from seeded worlds.

Each fixture is {fault, seed, t0, faults, incident, responses} where
responses are Responses API outputs (output arrays with usage) for a good
investigation. Cited insertIds and tool values come from the seeded world;
incident deployments/tripwires are the pinned D5 set; fault types, seeds,
at offsets and withinSeconds are derived from the scenario that expects
the same tripwire.
"""

import argparse
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)

# Pinned D5 incidents: fault -> (deployment, tripwire).
PINNED = {
    "F1": ("alert-ingestion/alpha", "dead-connector"),
    "F2": ("alert-ingestion/bravo", "connector-errors"),
    "F3": ("alert-ingestion/alpha", "priority-shape"),
    "F4": ("intel-sync/alpha", "hollow-run"),
    "F5": ("intel-sync/bravo", "job-errors"),
    "F7": ("intel-sync/alpha", "dead-job"),
}

# Right-answer categories per mocks.md §6 (test expectation, not world data).
CATEGORY = {
    "F1": "connector_stalled",
    "F2": "credential_failure",
    "F3": "schema_drift",
    "F4": "job_hollow_success",
    "F5": "job_failing",
    "F7": "job_stalled",
}


# Fault types per mocks.md §6 (setup input; seeds/offsets still derived).
FAULT_TYPE = {
    "F1": "connector_stall",
    "F2": "credential_expiry",
    "F3": "severity_drift",
    "F4": "job_hollow_success",
    "F5": "job_error",
    "F7": "job_stall",
}


def _load_scenario_for_fault(fault: str, tripwire: str) -> Any:
    from fleetsre.harness.scenario import load_scenario

    want = FAULT_TYPE[fault]
    fallback = None
    for path in sorted((ROOT / "fixtures" / "scenarios").rglob("*.yaml")):
        scenario = load_scenario(path)
        trips = [t.tripwire for t in scenario.expected.trips]
        if tripwire not in trips:
            continue
        if not scenario.faults or scenario.faults[0].type != want:
            continue
        if scenario.faults[0].clearAt is None:
            return scenario
        fallback = scenario
    if fallback is not None:
        return fallback
    raise AssertionError(f"no scenario for {fault} {tripwire!r}")


def _trip_until(
    world: Any, evaluator: Any, clock: Any, sink: Any,
    deployment: str, tripwire: str,
) -> Any:
    consumed = 0
    tick = 15
    elapsed = 0
    while elapsed <= 24 * 3600:
        tick_at = T0 + timedelta(seconds=elapsed)
        clock.set(tick_at)
        world.step_until(tick_at)
        events: list[Any] = []
        while consumed < len(sink.entries):
            events.extend(evaluator.ingest(sink.entries[consumed]))
            consumed += 1
        events.extend(evaluator.tick())
        for event in events:
            if (
                event.kind == "trip"
                and event.deployment == deployment
                and event.tripwire == tripwire
            ):
                clock.set(event.at)
                return event
        elapsed += tick
    raise AssertionError(f"no trip for {deployment} {tripwire}")


def _entries_for(sink: Any, env: str) -> list[Any]:
    return [
        e for e in sink.entries
        if e.jsonPayload.get("environment") == env
    ]


def _latest(
    entries: list[Any], now: datetime, pred: Any,
) -> Any:
    best = None
    for entry in entries:
        if entry.timestamp > now:
            continue
        if not pred(entry):
            continue
        if best is None or entry.timestamp >= best.timestamp:
            best = entry
    assert best is not None
    return best


def _by_insert(entries: list[Any], insert_id: str) -> Any:
    for entry in entries:
        if entry.insertId == insert_id:
            return entry
    raise AssertionError(f"insertId not in world: {insert_id!r}")


def _connector_name(fleet: Any, env: str) -> str:
    for integ, spec in fleet.integrations.items():
        if isinstance(spec, dict) and "connectors" in spec:
            conn_key = next(iter(spec["connectors"]))
            inst = fleet.environments[env].deployments[
                "alert-ingestion"
            ].connectorInstance
            return (
                f"{fleet.instance}/integrations/{integ}/connectors/"
                f"{conn_key}/connectorInstances/{inst}"
            )
    raise AssertionError("no connector integration")


def _job_name(fleet: Any, env: str) -> str:
    for integ, spec in fleet.integrations.items():
        if isinstance(spec, dict) and "jobs" in spec:
            job_key = next(iter(spec["jobs"]))
            inst = fleet.environments[env].deployments[
                "intel-sync"
            ].jobInstance
            return (
                f"{fleet.instance}/integrations/{integ}/jobs/"
                f"{job_key}/jobInstances/{inst}"
            )
    raise AssertionError("no job integration")


def _sentinel_integration_name(fleet: Any, env: str) -> str:
    for integ, spec in fleet.integrations.items():
        if isinstance(spec, dict) and "connectors" in spec:
            return (
                f"{fleet.instance}/integrations/{integ}/"
                f"integrationInstances/{env}"
            )
    raise AssertionError("no connector integration")


def _call(turn: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": f"call_{turn:03d}",
        "name": name,
        "arguments": json.dumps(args),
    }


def _reasoning(turn: int) -> dict[str, Any]:
    return {
        "type": "reasoning",
        "id": f"rs_{turn:03d}",
        "encrypted_content": f"enc-placeholder-{turn}",
    }


def _usage(turn: int) -> dict[str, Any]:
    return {
        "input_tokens": 8000 + turn * 500,
        "output_tokens": 300,
        "input_tokens_details": {"cached_tokens": 5000 + turn * 200},
        "output_tokens_details": {"reasoning_tokens": 150},
    }


def _response(turn: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": [_reasoning(turn), _call(turn, name, args)],
        "usage": _usage(turn),
    }


def _incident_id(service: str, env: str, tripwire: str, at: datetime) -> str:
    return (
        f"inc-{service}-{env}-{tripwire}-{at:%Y%m%dT%H%M%SZ}"
    )


def _verify_seconds(
    contract: Any, tripwire: Any, parameters: dict[str, Any],
    sink_entries: list[Any], env: str, trip_at: datetime,
) -> int:
    """Recovery rule x1.5, rounded up to a minute (review pin 20)."""
    import math

    def _resolve(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("${"):
            return parameters[value[2:-1]]
        return value

    spec = tripwire.spec
    base = 0.0
    if tripwire.type == "cadence":
        interval = int(_resolve(spec["intervalSeconds"]))
        base = float(2 * interval)
    elif tripwire.type == "errorSignature":
        interval = int(parameters["intervalSeconds"])
        window = int(_resolve(spec["windowRuns"]))
        threshold = int(_resolve(spec["threshold"]))
        base = float((window - threshold + 1) * interval + interval)
    else:
        recover_after = int(spec.get(
            "recoverAfter", spec.get("consecutive", 1)
        ))
        event = spec.get("event")
        count = sum(
            1 for e in sink_entries
            if e.jsonPayload.get("environment") == env
            and e.timestamp <= trip_at
            and (event is None or e.jsonPayload.get("event") == event)
        )
        duration = max((trip_at - T0).total_seconds(), 1)
        rate = count / duration
        base = recover_after / max(rate, 1e-9)
    return int(math.ceil(base * 1.5 / 60) * 60)


def _statements(fault: str, env: str) -> tuple[str, str]:
    correct = {
        "F1": f"The {env} connector stalled and stopped running.",
        "F2": f"The {env} connector credential expired; upstream 401s.",
        "F3": f"Upstream severity drift broke {env} priority types.",
        "F4": f"The {env} job runs hollow with Lookback Hours at 0.",
        "F5": f"The {env} job fails on an upstream feed outage.",
        "F7": f"The {env} job stalled and stopped running.",
    }
    alt = "A fleet-wide upstream outage hit every tenant alike."
    return correct[fault], alt


def build_fixture(fault: str) -> dict[str, Any]:
    from fleetsre.clock import SimClock
    from fleetsre.contracts import load_contract
    from fleetsre.evaluator.engine import Evaluator
    from fleetsre.mock.fleet import load_fleet
    from fleetsre.mock.world import World
    from fleetsre.ports import MemorySink
    from fleetsre.registry import deployments_from_fleet

    deployment, tripwire_id = PINNED[fault]
    service, env = deployment.split("/", 1)
    scenario = _load_scenario_for_fault(fault, tripwire_id)
    seed = scenario.seed
    spec_fault = scenario.faults[0]
    at = spec_fault.at
    ftype = spec_fault.type
    params = dict(spec_fault.params)

    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / f"{service}.json")
    tripwire = next(t for t in contract.tripwires if t.id == tripwire_id)
    component = tripwire.component
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(fleet, seed, clock, sink)
    world.schedule([{
        "at": at, "type": ftype, "environment": env, "params": params,
    }])
    deployments = deployments_from_fleet(fleet, contract)
    evaluator = Evaluator([contract], deployments, clock)
    trip = _trip_until(world, evaluator, clock, sink, deployment, tripwire_id)
    now = trip.at
    incident_id = _incident_id(service, env, tripwire_id, now)

    siblings = sorted(
        d.environment for d in deployments if d.environment != env
    )
    sib_env = siblings[0]
    sib_deployment = f"{service}/{sib_env}"

    own = _entries_for(sink, env)
    sib = _entries_for(sink, sib_env)
    evidence_ids = list(trip.evidence)
    if evidence_ids:
        key_entry = _by_insert(
            own, sorted(
                evidence_ids,
                key=lambda i: _by_insert(own, i).timestamp,
            )[-1],
        )
    else:
        key_entry = _latest(own, now, lambda e: True)
    key_id = key_entry.insertId

    if service == "alert-ingestion":
        hb_event = next(
            t.spec["event"] for t in contract.tripwires
            if t.type == "cadence"
        )
        sib_healthy = _latest(
            sib, now,
            lambda e: e.jsonPayload.get("event") == hb_event
            and e.severity == "INFO",
        )
    else:
        sib_healthy = _latest(
            sib, now,
            lambda e: e.jsonPayload.get("event") == "job.run.completed"
            and e.jsonPayload.get("status") == "SUCCESS",
        )
    sib_id = sib_healthy.insertId

    h1, h2 = _statements(fault, env)
    responses: list[dict[str, Any]] = []
    turn = 1
    responses.append(_response(turn, "set_hypotheses", {
        "items": [
            {"id": "H1", "statement": h1,
             "wouldConfirm": "incident signals point at this tenant",
             "alternative": False},
            {"id": "H2", "statement": h2,
             "wouldConfirm": "siblings show the same failure",
             "alternative": True},
        ],
        "rationale": "State the tenant fault and a fleet-wide alternative.",
    }))
    turn += 1
    if fault in ("F1", "F7"):
        hb = tripwire.spec["event"]
        filt: dict[str, Any] = {"component": component, "event": hb}
    elif fault == "F2":
        filt = {"component": component, "minSeverity": "WARNING"}
    elif fault == "F3":
        filt = {"component": component, "event": "alert.ingested"}
    elif fault == "F4":
        filt = {"component": component, "event": "job.run.completed"}
    else:
        filt = {"component": component, "minSeverity": "ERROR"}
    responses.append(_response(turn, "query_logs", {
        "deployment": deployment,
        "window": {"lastMinutes": 120},
        "filter": filt,
        "rationale": "Read the incident deployment around the trip.",
    }))
    turn += 1
    responses.append(_response(turn, "record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {"insertId": key_id},
        "why": "key trip signal from the incident deployment",
        "rationale": "Pin the trip signal to the tenant hypothesis.",
    }))
    turn += 1
    responses.append(_response(turn, "compare_siblings", {
        "service": service, "component": component,
        "window": {"lastMinutes": 120},
        "rationale": "Check whether siblings fail the same way.",
    }))
    turn += 1
    if service == "alert-ingestion":
        sib_filter: dict[str, Any] = {
            "component": component, "event": hb_event,
        }
    else:
        sib_filter = {"component": component, "event": "job.run.completed"}
    responses.append(_response(turn, "query_logs", {
        "deployment": sib_deployment,
        "window": {"lastMinutes": 120},
        "filter": sib_filter,
        "rationale": "Read a sibling to test the fleet-wide idea.",
    }))
    turn += 1
    responses.append(_response(turn, "record_evidence", {
        "hypothesis": "H2", "stance": "refutes",
        "source": {"insertId": sib_id},
        "why": "sibling runs healthy on schedule",
        "rationale": "Healthy sibling refutes the fleet-wide hypothesis.",
    }))
    turn += 1
    tool_call_id = f"call_{turn:03d}"
    if fault == "F2":
        integ_name = _sentinel_integration_name(fleet, env)
        tested = world.execute_test(integ_name)
        tool_field = "successful"
        tool_value: Any = tested["successful"]
        responses.append(_response(turn, "test_integration_instance", {
            "name": integ_name,
            "rationale": "Test the incident tenant connectivity directly.",
        }))
    else:
        if service == "alert-ingestion":
            res_name = _connector_name(fleet, env)
            res = world.get_connector(res_name)
        else:
            res_name = _job_name(fleet, env)
            res = world.get_job(res_name)
        if fault == "F1":
            tool_field, tool_value = "status", res["status"]
        elif fault == "F3":
            idx = next(
                i for i, p in enumerate(res["parameters"])
                if p.get("value") == ""
            )
            disp = res["parameters"][idx]["displayName"]
            assert disp == "Unknown Severity Priority"
            tool_field, tool_value = (
                f"parameters.{idx}.value", res["parameters"][idx]["value"]
            )
        elif fault == "F4":
            idx = next(
                i for i, p in enumerate(res["parameters"])
                if p.get("value") == "0"
            )
            disp = res["parameters"][idx]["displayName"]
            assert disp == "Lookback Hours"
            tool_field, tool_value = (
                f"parameters.{idx}.value", res["parameters"][idx]["value"]
            )
        elif fault == "F5":
            tool_field, tool_value = (
                "lastRunStatus", res["lastRunStatus"]
            )
        else:
            tool_field, tool_value = "enabled", res["enabled"]
        responses.append(_response(turn, "get_resource", {
            "name": res_name,
            "rationale": "Read the incident resource state directly.",
        }))
    turn += 1
    responses.append(_response(turn, "record_evidence", {
        "hypothesis": "H1", "stance": "supports",
        "source": {
            "callId": tool_call_id, "field": tool_field,
            "value": tool_value,
        },
        "why": "resource state backs the tenant hypothesis",
        "rationale": "Pin the resource signal to the tenant hypothesis.",
    }))
    turn += 1
    category = CATEGORY[fault]
    if fault in ("F2", "F5"):
        action: dict[str, Any] = {
            "type": "escalate",
            "escalationSummary": (
                "Fix is outside the allowlist; needs a human."
            ),
        }
        diagnosis: dict[str, Any] = {
            "incidentId": incident_id,
            "deployment": deployment,
            "component": component,
            "category": category,
            "hypothesis": "H1",
            "summary": h1,
            "evidence": ["E1", "E3"],
            "openQuestions": [],
            "action": action,
            "confidence": "high",
        }
    else:
        if fault == "F1":
            method = "connectorInstances.restart"
            resource = _connector_name(fleet, env)
            mask: list[str] = []
            patch: dict[str, Any] = {}
        elif fault == "F3":
            method = "connectorInstances.patch"
            resource = _connector_name(fleet, env)
            mask = ["parameters"]
            full = copy.deepcopy(
                world.get_connector(resource)["parameters"]
            )
            for param in full:
                if param.get("displayName") == "Unknown Severity Priority":
                    param["value"] = "100"
            patch = {"parameters": full}
        elif fault == "F4":
            method = "jobInstances.patch"
            resource = _job_name(fleet, env)
            mask = ["parameters"]
            full = copy.deepcopy(
                world.get_job(resource)["parameters"]
            )
            for param in full:
                if param.get("displayName") == "Lookback Hours":
                    param["value"] = "24"
            patch = {"parameters": full}
        else:
            method = "jobInstances.restart"
            resource = _job_name(fleet, env)
            mask = []
            patch = {}
        dep_params = next(
            d.parameters for d in deployments
            if d.id == deployment
        )
        seconds = _verify_seconds(
            contract, tripwire, dict(dep_params), sink.entries,
            env, now,
        )
        print(f"{fault}: expectedVerification.seconds = {seconds}")
        diagnosis = {
            "incidentId": incident_id,
            "deployment": deployment,
            "component": component,
            "category": category,
            "hypothesis": "H1",
            "summary": h1,
            "evidence": ["E1", "E3"],
            "openQuestions": [],
            "action": {
                "type": "propose", "method": method,
                "resource": resource, "updateMask": mask,
                "patch": patch,
            },
            "expectedVerification": {
                "tripwire": tripwire_id, "condition": "recovers_within",
                "seconds": seconds,
            },
            "confidence": "high",
        }
    responses.append(_response(turn, "submit_diagnosis", {
        "diagnosis": diagnosis,
        "rationale": "Submit the right-answer diagnosis.",
    }))

    return {
        "fault": fault,
        "seed": seed,
        "t0": "2026-10-29T14:00:00Z",
        "faults": [{
            "at": at, "type": ftype, "environment": env,
            "params": params,
        }],
        "incident": {
            "id": incident_id, "deployment": deployment,
            "tripwire": tripwire_id,
        },
        "responses": responses,
    }


def _rebuild(fault: str) -> dict[str, Any]:
    """Rebuild a fixture's world and freeze it at the recorded trip."""
    from fleetsre.clock import SimClock
    from fleetsre.contracts import load_contract
    from fleetsre.evaluator.engine import Evaluator
    from fleetsre.mock.fleet import load_fleet
    from fleetsre.mock.world import World
    from fleetsre.ports import MemorySink
    from fleetsre.registry import deployments_from_fleet

    path = ROOT / "fixtures" / "scripted" / f"{fault}.json"
    fixture = json.loads(path.read_text())
    deployment = fixture["incident"]["deployment"]
    service = deployment.split("/", 1)[0]
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / f"{service}.json")
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(fleet, fixture["seed"], clock, sink)
    world.schedule(fixture["faults"])
    deployments = deployments_from_fleet(fleet, contract)
    evaluator = Evaluator([contract], deployments, clock)
    consumed = 0
    elapsed = 0
    while elapsed <= 24 * 3600:
        tick_at = T0 + timedelta(seconds=elapsed)
        clock.set(tick_at)
        world.step_until(tick_at)
        events: list[Any] = []
        while consumed < len(sink.entries):
            events.extend(evaluator.ingest(sink.entries[consumed]))
            consumed += 1
        events.extend(evaluator.tick())
        for event in events:
            if (
                event.kind == "trip"
                and event.deployment == deployment
                and event.tripwire == fixture["incident"]["tripwire"]
            ):
                clock.set(event.at)
                return {
                    "fixture": fixture, "world": world, "clock": clock,
                    "sink": sink, "evaluator": evaluator, "trip": event,
                    "deployments": deployments, "contract": contract,
                    "fleet": fleet,
                }
        elapsed += 15
    raise AssertionError(f"no trip for {fault}")


def _route_ids(entry: Any, contract: Any, deployments: list[Any]) -> set[str]:
    """Evaluator-style routing: container + match labels + deploymentBy."""
    container = entry.resource.labels.get("container_name")
    found: set[str] = set()
    by_instance: dict[tuple[str, str], dict[str, str]] = {}
    for dep in deployments:
        for comp_id, instance in dep.resources.items():
            by_instance.setdefault(
                (dep.service, comp_id), {}
            )[instance] = dep.id
    for component in contract.components:
        if component.logs.container_name != container:
            continue
        if any(
            key not in entry.labels for key in component.logs.match
        ):
            continue
        by = component.logs.deploymentBy
        if by.startswith("labels."):
            value = entry.labels.get(by[len("labels."):])
            if value is None:
                continue
            dep_id = by_instance.get(
                (contract.service, component.id), {}
            ).get(value)
            if dep_id is not None:
                found.add(dep_id)
        elif by.startswith("jsonPayload."):
            node: Any = entry.jsonPayload
            for part in by.split(".")[1:]:
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    node = None
                    break
            if isinstance(node, str):
                found.add(f"{contract.service}/{node}")
    if not found:
        env = entry.jsonPayload.get("environment")
        if isinstance(env, str):
            found.add(f"{contract.service}/{env}")
    return found


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


def _check_fixture(fault: str) -> list[str]:
    """Rebuild and verify one scripted fixture; returns problem lines."""
    from fleetsre.contracts import SchemaSpec
    from fleetsre.evaluator import tripwires as tripmod

    setup = _rebuild(fault)
    fixture = setup["fixture"]
    world = setup["world"]
    sink = setup["sink"]
    trip = setup["trip"]
    contract = setup["contract"]
    deployments = setup["deployments"]
    problems: list[str] = []
    deployment = fixture["incident"]["deployment"]
    service, env = deployment.split("/", 1)
    by_id = {e.insertId: e for e in sink.entries}
    assert fixture["incident"]["id"] == _incident_id(
        service, env, fixture["incident"]["tripwire"], trip.at
    ), fault

    calls: dict[str, dict[str, Any]] = {}
    for response in fixture["responses"]:
        for item in response.get("output", []):
            if item.get("type") != "function_call":
                continue
            args = json.loads(item["arguments"])
            calls[item["call_id"]] = {
                "name": item["name"], "args": args,
            }
    evidence_order: list[str] = []
    for response in fixture["responses"]:
        for item in response.get("output", []):
            if item.get("type") != "function_call":
                continue
            if item["name"] != "record_evidence":
                continue
            args = json.loads(item["arguments"])
            evidence_order.append(item["call_id"])
            source = args["source"]
            if "insertId" in source:
                entry = by_id.get(source["insertId"])
                if entry is None:
                    problems.append(
                        f"{item['call_id']}: insertId missing"
                    )
                    continue
                if entry.timestamp > trip.at:
                    problems.append(
                        f"{item['call_id']}: insertId after trip"
                    )
                routed = _route_ids(entry, contract, deployments)
                if deployment not in routed and not (
                    routed - {deployment}
                ):
                    problems.append(
                        f"{item['call_id']}: routes nowhere"
                    )
            else:
                target = calls.get(source["callId"])
                if target is None:
                    problems.append(
                        f"{item['call_id']}: callId missing"
                    )
                    continue
                if target["name"] == "test_integration_instance":
                    live: Any = world.execute_test(
                        target["args"]["name"]
                    )
                elif target["name"] == "get_resource":
                    name = target["args"]["name"]
                    if "connectorInstances" in name:
                        live = world.get_connector(name)
                    else:
                        live = world.get_job(name)
                else:
                    problems.append(
                        f"{item['call_id']}: uncited tool "
                        f"{target['name']}"
                    )
                    continue
                found, value = _resolve_field(live, source["field"])
                if not found or value != source["value"] or (
                    isinstance(value, bool)
                    and type(value) is not type(source["value"])
                ):
                    problems.append(
                        f"{item['call_id']}: tool value differs"
                    )
    submits = [
        json.loads(item["arguments"])["diagnosis"]
        for response in fixture["responses"]
        for item in response.get("output", [])
        if item.get("type") == "function_call"
        and item.get("name") == "submit_diagnosis"
    ]
    assert len(submits) == 1, fault
    diagnosis = submits[0]
    cited = diagnosis["evidence"]
    log_ids = []
    for eid in cited:
        idx = int(eid[1:]) - 1
        call_id = evidence_order[idx]
        args = calls[call_id]["args"]
        if "insertId" in args["source"]:
            log_ids.append(args["source"]["insertId"])
    category = diagnosis["category"]
    own = [e for e in sink.entries
           if e.jsonPayload.get("environment") == env
           and e.timestamp <= trip.at]
    if category in ("connector_stalled", "job_stalled"):
        event = next(
            t.spec["event"] for t in contract.tripwires
            if t.id == fixture["incident"]["tripwire"]
        )
        beats = [e for e in own
                 if e.jsonPayload.get("event") == event]
        latest = max(beats, key=lambda e: e.timestamp).insertId
        if latest not in log_ids:
            problems.append("category: latest heartbeat not cited")
    elif category == "credential_failure":
        warned = any(
            by_id[i].severity in ("WARNING", "ERROR") for i in log_ids
        )
        tested = False
        for eid in cited:
            src = calls[evidence_order[int(eid[1:]) - 1]]["args"]["source"]
            cid = src.get("callId", "")
            if cid and calls[cid]["name"] == "test_integration_instance":
                live = world.execute_test(calls[cid]["args"]["name"])
                if live["successful"] is False:
                    tested = True
        if not warned or not tested:
            problems.append("category: credential evidence incomplete")
    elif category == "schema_drift":
        spec = next(
            t.spec for t in contract.tripwires
            if t.id == fixture["incident"]["tripwire"]
        )
        schema = SchemaSpec(
            event=spec.get("event"), field=spec["field"],
            type=spec["type"], enum=spec.get("enum"),
            minimum=spec.get("minimum"),
            required=bool(spec["required"]),
            consecutive=int(spec.get("consecutive", 1)),
            recoverAfter=spec.get("recoverAfter"),
            where=spec.get("where"),
        )
        if not any(
            tripmod.schema_violation(by_id[i], schema) is not None
            for i in log_ids
        ):
            problems.append("category: no violating entry cited")
    elif category == "job_hollow_success":
        if not any(
            by_id[i].jsonPayload.get("status") == "SUCCESS"
            and by_id[i].jsonPayload.get("indicatorsSynced") == 0
            for i in log_ids
        ):
            problems.append("category: no hollow entry cited")
    elif category == "job_failing":
        if not any(
            by_id[i].jsonPayload.get("event") in (
                "job.run.error", "job.run.completed")
            and (
                by_id[i].jsonPayload.get("event") == "job.run.error"
                or by_id[i].jsonPayload.get("status") == "ERROR"
            )
            for i in log_ids
        ):
            problems.append("category: no error entry cited")
    # T7 near-miss candidates.
    if category in ("connector_stalled", "job_stalled"):
        event = next(
            t.spec["event"] for t in contract.tripwires
            if t.id == fixture["incident"]["tripwire"]
        )
        beats = [e for e in own
                 if e.jsonPayload.get("event") == event]
        if len(beats) < 2:
            problems.append("near-miss: no older heartbeat")
    if fault == "F3":
        valid = [
            e for e in own
            if e.jsonPayload.get("event") == "alert.ingested"
            and isinstance(
                (e.jsonPayload.get("alert") or {}).get("priority"), int
            )
        ]
        if not valid:
            problems.append("near-miss: no valid ingested entry")
    if fault == "F4":
        hollow = [
            e for e in own
            if e.jsonPayload.get("event") == "job.run.completed"
            and e.jsonPayload.get("status") == "SUCCESS"
            and e.jsonPayload.get("indicatorsSynced") == 0
        ]
        if not hollow:
            problems.append("near-miss: no hollow entry to copy")
    if fault == "F5":
        good = [
            e for e in own
            if e.jsonPayload.get("event") == "job.run.completed"
            and e.jsonPayload.get("status") == "SUCCESS"
        ]
        if not good:
            problems.append("near-miss: no SUCCESS completed entry")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate scripted D5 data")
    parser.add_argument(
        "--check", action="store_true",
        help="rebuild each world and verify its fixture",
    )
    args = parser.parse_args(argv)
    if args.check:
        failed = False
        for fault in ("F1", "F2", "F3", "F4", "F5", "F7"):
            problems = _check_fixture(fault)
            if problems:
                failed = True
                print(f"{fault}: FAIL")
                for line in problems:
                    print(f"  {line}")
            else:
                print(f"{fault}: OK")
        return 1 if failed else 0
    out_dir = ROOT / "fixtures" / "scripted"
    out_dir.mkdir(parents=True, exist_ok=True)
    for fault in ("F1", "F2", "F3", "F4", "F5", "F7"):
        fixture = build_fixture(fault)
        path = out_dir / f"{fault}.json"
        path.write_text(json.dumps(fixture, indent=2) + "\n")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
