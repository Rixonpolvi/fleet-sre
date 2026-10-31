"""D4 acceptance tests: evaluator and chaos harness."""

import builtins
import configparser
import copy
import json
import random
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from fleetsre.clock import SimClock
from fleetsre.contracts import (
    Contract,
    ErrorSigSpec,
    SchemaSpec,
    load_contract,
)
from fleetsre.evaluator.canary import CanaryScheduler
from fleetsre.evaluator.engine import Evaluator, TripEvent
from fleetsre.evaluator.tripwires import (
    cadence_due,
    canary_missed,
    error_signature,
    schema_violation,
)
from fleetsre.harness.replay import ScenarioResult, run_scenario
from fleetsre.harness.scenario import Scenario, load_scenario
from fleetsre.harness.scorecard import Scorecard, scorecard
from fleetsre.logmodel import LogEntry, read_jsonl
from fleetsre.mock.fleet import load_fleet
from fleetsre.mock.world import FaultSpec, World
from fleetsre.ports import MemorySink
from fleetsre.registry import Deployment

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)

# Cloud Logging severity order (pinned semantics §6): worst entry wins.
_SEVERITY_ORDER = {
    "DEBUG": 0,
    "INFO": 1,
    "NOTICE": 2,
    "WARNING": 3,
    "ERROR": 4,
    "CRITICAL": 5,
    "ALERT": 6,
    "EMERGENCY": 7,
}


def _contract(service: str) -> Contract:
    return load_contract(ROOT / "contracts" / f"{service}.json")


def _fleet() -> Any:
    return load_fleet(ROOT / "fixtures" / "fleet.yaml")


def _reference_services() -> list[str]:
    services: list[str] = []
    for path in sorted((ROOT / "contracts").glob("*.json")):
        if path.name == "schema.json":
            continue
        services.append(load_contract(path).service)
    return sorted(services)


def _tripwire_by_type(contract: Contract, tw_type: str) -> Any:
    for tripwire in contract.tripwires:
        if tripwire.type == tw_type:
            return tripwire
    raise AssertionError(f"no {tw_type} tripwire in {contract.service}")


def _resolve(value: Any, parameters: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return parameters[value[2:-1]]
    return value


def _fleet_params(service: str, env: str) -> dict[str, Any]:
    fleet = _fleet()
    return dict(fleet.environments[env].deployments[service].parameters)


def _envs_for_service(service: str) -> list[str]:
    fleet = _fleet()
    return sorted(
        env
        for env, spec in fleet.environments.items()
        if service in spec.deployments
    )


def _deployments_for_service(service: str) -> list[Deployment]:
    return [
        Deployment(
            id=f"{service}/{env}",
            service=service,
            environment=env,
            parameters=_fleet_params(service, env),
        )
        for env in _envs_for_service(service)
    ]


def _deployments_for_scenario(scenario: Scenario) -> list[Deployment]:
    return [
        Deployment(
            id=f"{scenario.service}/{env}",
            service=scenario.service,
            environment=env,
            parameters=_fleet_params(scenario.service, env),
        )
        for env in scenario.deployments
    ]


def _deployments_for_ids(service: str, ids: list[str]) -> list[Deployment]:
    deployments: list[Deployment] = []
    for dep_id in ids:
        svc, env = dep_id.split("/", 1)
        assert svc == service
        deployments.append(
            Deployment(
                id=dep_id,
                service=service,
                environment=env,
                parameters=_fleet_params(service, env),
            )
        )
    return deployments


def _schema_spec(tripwire: Any) -> SchemaSpec:
    spec = dict(tripwire.spec)
    consecutive = int(spec.get("consecutive", 1))
    recover_after = spec.get("recoverAfter")
    return SchemaSpec(
        event=spec.get("event"),
        field=spec["field"],
        type=spec["type"],
        enum=spec.get("enum"),
        minimum=spec.get("minimum"),
        required=bool(spec["required"]),
        consecutive=consecutive,
        recoverAfter=None if recover_after is None else int(recover_after),
        where=spec.get("where"),
    )


def _error_spec(tripwire: Any, parameters: dict[str, Any] | None = None) -> ErrorSigSpec:
    spec = dict(tripwire.spec)
    params = parameters or {}
    return ErrorSigSpec(
        minSeverity=str(spec["minSeverity"]),
        windowRuns=int(_resolve(spec["windowRuns"], params)),
        threshold=int(_resolve(spec["threshold"], params)),
    )


def _cadence_params(
    contract: Contract, service: str, env: str
) -> tuple[int, int, str]:
    tripwire = _tripwire_by_type(contract, "cadence")
    params = _fleet_params(service, env)
    interval_s = int(_resolve(tripwire.spec["intervalSeconds"], params))
    max_silent = int(_resolve(tripwire.spec["maxSilentIntervals"], params))
    return interval_s, max_silent, str(tripwire.spec["event"])


def _synthetic_params(contract: Contract) -> tuple[int, int, str, str]:
    tripwire = _tripwire_by_type(contract, "synthetic")
    params: dict[str, Any] = {}
    every = int(_resolve(tripwire.spec["everyMinutes"], params))
    deadline = int(_resolve(tripwire.spec["deadlineSeconds"], params))
    return every, deadline, str(tripwire.id), str(tripwire.component)


def _load_scenario(service: str, sid: str) -> Scenario:
    return load_scenario(ROOT / "fixtures" / "scenarios" / service / f"{sid}.yaml")


def _scenarios_for_service(service: str) -> list[Scenario]:
    return [
        load_scenario(path)
        for path in sorted((ROOT / "fixtures" / "scenarios" / service).glob("*.yaml"))
    ]


def _all_sample_entries() -> list[LogEntry]:
    entries: list[LogEntry] = []
    for path in sorted((ROOT / "fixtures" / "logs" / "samples").glob("*.jsonl")):
        entries.extend(read_jsonl(path))
    return entries


def _base_entry(event: str) -> LogEntry:
    for entry in _all_sample_entries():
        if entry.jsonPayload.get("event") == event:
            return entry
    raise AssertionError(f"no sample entry with event {event!r}")


def _clone_entry(
    base: LogEntry,
    *,
    timestamp: datetime,
    insert_id: str,
    env: str,
    service: str,
    run_id: str | None = None,
    severity: str | None = None,
) -> LogEntry:
    labels = copy.deepcopy(base.labels)
    payload = copy.deepcopy(base.jsonPayload)
    fleet = _fleet()
    if "connector_instance" in labels:
        instance = fleet.environments[env].deployments[service].connectorInstance
        labels["connector_instance"] = str(instance)
    payload["environment"] = env
    alert = payload.get("alert")
    if isinstance(alert, dict):
        alert["environment"] = env
    if run_id is not None and "runId" in payload:
        payload["runId"] = run_id
    update: dict[str, Any] = {
        "insertId": insert_id,
        "timestamp": timestamp,
        "labels": labels,
        "jsonPayload": payload,
    }
    if severity is not None:
        update["severity"] = severity
    return base.model_copy(update=update)


def _drain(
    evaluator: Evaluator, entries: list[LogEntry]
) -> list[TripEvent]:
    events: list[TripEvent] = []
    for entry in entries:
        out = evaluator.ingest(entry)
        assert isinstance(out, list)
        for event in out:
            assert isinstance(event, TripEvent)
        events.extend(out)
    return events


@pytest.fixture(scope="module")
def _ref_contracts() -> dict[str, Contract]:
    contracts: dict[str, Contract] = {}
    for path in sorted((ROOT / "contracts").glob("*.json")):
        if path.name == "schema.json":
            continue
        contract = load_contract(path)
        contracts[contract.service] = contract
    return contracts


@pytest.fixture(scope="module")
def _ref_scenarios(
    _ref_contracts: dict[str, Contract],
) -> dict[str, list[Scenario]]:
    return {
        service: _scenarios_for_service(service) for service in _ref_contracts
    }


@pytest.fixture(scope="module")
def _ref_scorecards(
    _ref_contracts: dict[str, Contract],
    _ref_scenarios: dict[str, list[Scenario]],
) -> dict[str, Scorecard]:
    return {
        service: scorecard(contract, _ref_scenarios[service])
        for service, contract in _ref_contracts.items()
    }


@pytest.fixture(scope="module")
def _mutant_cases(
    _ref_scenarios: dict[str, list[Scenario]],
) -> list[tuple[str, Contract, list[Scenario]]]:
    cases: list[tuple[str, Contract, list[Scenario]]] = []
    for path in sorted((ROOT / "fixtures" / "mutants").glob("*.json")):
        contract = load_contract(path)
        cases.append((path.name, contract, _ref_scenarios[contract.service]))
    return cases


@pytest.fixture(scope="module")
def _mutant_scorecards(
    _mutant_cases: list[tuple[str, Contract, list[Scenario]]],
) -> list[tuple[str, Scorecard]]:
    return [
        (name, scorecard(contract, scenarios))
        for name, contract, scenarios in _mutant_cases
    ]


def test_d4_t1_cadence_boundaries():
    """D4-T1: cadence_due flips at max_silent intervals, from armed_at when empty."""
    for service in _reference_services():
        contract = _contract(service)
        try:
            _tripwire_by_type(contract, "cadence")
        except AssertionError:
            continue
        env = _envs_for_service(service)[0]
        interval_s, max_silent, _event = _cadence_params(contract, service, env)
        window = max_silent * interval_s
        armed_at = T0
        last = armed_at + timedelta(seconds=interval_s)
        before = last + timedelta(seconds=window - 1)
        after = last + timedelta(seconds=window + 1)
        assert cadence_due(last, armed_at, before, interval_s, max_silent) is False
        assert cadence_due(last, armed_at, after, interval_s, max_silent) is True
        empty_before = armed_at + timedelta(seconds=window - 1)
        empty_after = armed_at + timedelta(seconds=window + 1)
        assert (
            cadence_due(None, armed_at, empty_before, interval_s, max_silent)
            is False
        )
        assert (
            cadence_due(None, armed_at, empty_after, interval_s, max_silent) is True
        )


def test_d4_t2_quiet_tenant():
    """D4-T2: quiet-tenant replays with zero trips on its reference contract."""
    matches = sorted((ROOT / "fixtures" / "scenarios").rglob("quiet-tenant.yaml"))
    assert len(matches) == 1
    scenario = load_scenario(matches[0])
    contract = _contract(scenario.service)
    result = run_scenario(contract, scenario)
    assert isinstance(result, ScenarioResult)
    assert result.scenario_id == scenario.id
    assert result.actual == []
    assert result.missing == []
    assert result.unexpected == []
    assert result.passed is True


def test_d4_t3_canary_boundaries():
    """D4-T3: canary_missed flips at the deadline, seen-in-time never misses."""
    found = False
    for service in _reference_services():
        contract = _contract(service)
        try:
            _every, deadline_s, _tw_id, _comp = _synthetic_params(contract)
        except AssertionError:
            continue
        found = True
        planted_at = T0
        seen_in_time = planted_at + timedelta(seconds=deadline_s - 1)
        late_now = planted_at + timedelta(seconds=deadline_s + 1)
        assert (
            canary_missed(planted_at, seen_in_time, late_now, deadline_s) is False
        )
        assert canary_missed(planted_at, None, late_now, deadline_s) is True
        early_now = planted_at + timedelta(seconds=deadline_s - 1)
        assert canary_missed(planted_at, None, early_now, deadline_s) is False
        seen_late = planted_at + timedelta(seconds=deadline_s + 1)
        now_after = planted_at + timedelta(seconds=deadline_s + 2)
        assert canary_missed(planted_at, seen_late, now_after, deadline_s) is True
    assert found


def _nested_del(payload: dict[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    node = payload
    for part in parts[:-1]:
        node = node[part]
    del node[parts[-1]]


def _nested_set(payload: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = payload
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def test_d4_t4_schema_detection():
    """D4-T4: schema_violation reasons and K-consecutive evaluator trips."""
    enum_tw: Any = None
    for service in _reference_services():
        contract = _contract(service)
        for tripwire in contract.tripwires:
            if tripwire.type == "schema" and tripwire.spec.get("enum") is not None:
                enum_tw = tripwire
    assert enum_tw is not None
    enum_spec = _schema_spec(enum_tw)
    assert enum_spec.event is not None
    base = _base_entry(str(enum_spec.event))
    assert schema_violation(base, enum_spec) is None
    field_path = str(enum_spec.field).removeprefix("jsonPayload.")
    missing_payload = copy.deepcopy(base.jsonPayload)
    _nested_del(missing_payload, field_path)
    missing_entry = base.model_copy(update={"jsonPayload": missing_payload})
    reason = schema_violation(missing_entry, enum_spec)
    assert isinstance(reason, str) and reason
    wrong_payload = copy.deepcopy(base.jsonPayload)
    _nested_set(wrong_payload, field_path, "wrong-type-string")
    wrong_entry = base.model_copy(update={"jsonPayload": wrong_payload})
    reason = schema_violation(wrong_entry, enum_spec)
    assert isinstance(reason, str) and reason
    assert enum_spec.enum is not None
    outside = None
    for candidate in (0, 1, 9999, -9999, 123456):
        if candidate not in enum_spec.enum:
            outside = candidate
            break
    assert outside is not None
    ooo_payload = copy.deepcopy(base.jsonPayload)
    _nested_set(ooo_payload, field_path, outside)
    ooo_entry = base.model_copy(update={"jsonPayload": ooo_payload})
    reason = schema_violation(ooo_entry, enum_spec)
    assert isinstance(reason, str) and reason

    where_tw: Any = None
    where_service = ""
    for service in _reference_services():
        contract = _contract(service)
        for tripwire in contract.tripwires:
            if tripwire.type == "schema" and tripwire.spec.get("where") is not None:
                where_tw = tripwire
                where_service = service
    assert where_tw is not None
    where_spec = _schema_spec(where_tw)
    where_contract = _contract(where_service)
    where_env = _envs_for_service(where_service)[0]
    where_dep_id = f"{where_service}/{where_env}"
    where_tw_id = str(where_tw.id)
    assert where_spec.event is not None
    assert where_spec.consecutive >= 2
    hollow_base = _base_entry(str(where_spec.event))
    minimum = where_spec.minimum
    assert minimum is not None

    def _hollow(
        clock_base: datetime,
        index: int,
        *,
        status: str,
        synced: Any,
        event: str | None = None,
    ) -> LogEntry:
        src = hollow_base
        if event is not None and event != hollow_base.jsonPayload.get("event"):
            src = _base_entry(event)
        entry = _clone_entry(
            src,
            timestamp=clock_base + timedelta(seconds=10 * index),
            insert_id=f"hollow-t4-{index:04d}-{status}",
            env=where_env,
            service=where_service,
            run_id=f"hollow-t4-run-{index:04d}",
        )
        entry.jsonPayload["event"] = event or str(where_spec.event)
        entry.jsonPayload["status"] = status
        entry.jsonPayload["indicatorsSynced"] = synced
        return entry

    clock = SimClock(T0)
    evaluator = Evaluator(
        [where_contract],
        [
            Deployment(
                id=where_dep_id,
                service=where_service,
                environment=where_env,
                parameters=_fleet_params(where_service, where_env),
            )
        ],
        clock,
    )
    for i in range(where_spec.consecutive - 1):
        entry = _hollow(T0, i, status="SUCCESS", synced=0)
        clock.set(entry.timestamp)
        events = evaluator.ingest(entry)
        events.extend(evaluator.tick())
        assert [e for e in events if e.tripwire == where_tw_id] == []
    trip_entry = _hollow(T0, where_spec.consecutive, status="SUCCESS", synced=0)
    clock.set(trip_entry.timestamp)
    trip_events = evaluator.ingest(trip_entry)
    trip_events.extend(evaluator.tick())
    trips = [
        e
        for e in trip_events
        if e.kind == "trip" and e.tripwire == where_tw_id
    ]
    assert len(trips) == 1
    assert trips[0].deployment == where_dep_id

    clock2 = SimClock(T0)
    evaluator2 = Evaluator(
        [where_contract],
        [
            Deployment(
                id=where_dep_id,
                service=where_service,
                environment=where_env,
                parameters=_fleet_params(where_service, where_env),
            )
        ],
        clock2,
    )
    seq: list[LogEntry] = []
    valid_synced = int(minimum) + 5
    seq.append(_hollow(T0, 100, status="SUCCESS", synced=0))
    seq.append(_hollow(T0, 101, status="SUCCESS", synced=0))
    seq.append(_hollow(T0, 102, status="SUCCESS", synced=valid_synced))
    seq.append(_hollow(T0, 103, status="SUCCESS", synced=0))
    seq.append(_hollow(T0, 104, status="SUCCESS", synced=0))
    for entry in seq:
        clock2.set(entry.timestamp)
        events = evaluator2.ingest(entry)
        events.extend(evaluator2.tick())
        assert [e for e in events if e.tripwire == where_tw_id] == []
    dwell_entry = _hollow(T0, 105, status="SUCCESS", synced=0)
    clock2.set(dwell_entry.timestamp)
    dwell = evaluator2.ingest(dwell_entry)
    dwell.extend(evaluator2.tick())
    assert (
        len(
            [
                e
                for e in dwell
                if e.kind == "trip" and e.tripwire == where_tw_id
            ]
        )
        == 1
    )

    clock3 = SimClock(T0)
    evaluator3 = Evaluator(
        [where_contract],
        [
            Deployment(
                id=where_dep_id,
                service=where_service,
                environment=where_env,
                parameters=_fleet_params(where_service, where_env),
            )
        ],
        clock3,
    )
    others = _base_entry("job.run.error")
    other = _clone_entry(
        others,
        timestamp=T0 + timedelta(seconds=1000),
        insert_id="hollow-t4-other-0001",
        env=where_env,
        service=where_service,
        run_id="hollow-t4-other-run",
        severity="ERROR",
    )
    got: list[TripEvent] = []
    first_other = _hollow(T0, 200, status="SUCCESS", synced=0)
    clock3.set(first_other.timestamp)
    got.extend(evaluator3.ingest(first_other))
    got.extend(evaluator3.tick())
    clock3.set(other.timestamp)
    got.extend(evaluator3.ingest(other))
    got.extend(evaluator3.tick())
    second_other = _hollow(T0, 201, status="SUCCESS", synced=0)
    clock3.set(second_other.timestamp)
    got.extend(evaluator3.ingest(second_other))
    got.extend(evaluator3.tick())
    assert [e for e in got if e.tripwire == where_tw_id] == []
    final_entry = _hollow(T0, 202, status="SUCCESS", synced=0)
    clock3.set(final_entry.timestamp)
    final = evaluator3.ingest(final_entry)
    final.extend(evaluator3.tick())
    assert (
        len(
            [
                e
                for e in final
                if e.kind == "trip" and e.tripwire == where_tw_id
            ]
        )
        == 1
    )

    clock4 = SimClock(T0)
    evaluator4 = Evaluator(
        [where_contract],
        [
            Deployment(
                id=where_dep_id,
                service=where_service,
                environment=where_env,
                parameters=_fleet_params(where_service, where_env),
            )
        ],
        clock4,
    )
    err_case: list[TripEvent] = []
    for idx, status in [(300, "SUCCESS"), (301, "SUCCESS"), (302, "ERROR")]:
        entry = _hollow(T0, idx, status=status, synced=0)
        clock4.set(entry.timestamp)
        err_case.extend(evaluator4.ingest(entry))
        err_case.extend(evaluator4.tick())
    assert [e for e in err_case if e.tripwire == where_tw_id] == []
    last_entry = _hollow(T0, 303, status="SUCCESS", synced=0)
    clock4.set(last_entry.timestamp)
    last = evaluator4.ingest(last_entry)
    last.extend(evaluator4.tick())
    assert (
        len([e for e in last if e.kind == "trip" and e.tripwire == where_tw_id])
        == 1
    )


def test_d4_t5_error_signature():
    """D4-T5: error_signature trips at threshold; one warning stays green."""
    for service in _reference_services():
        contract = _contract(service)
        try:
            tripwire = _tripwire_by_type(contract, "errorSignature")
        except AssertionError:
            continue
        env = _envs_for_service(service)[0]
        params = _fleet_params(service, env)
        spec = _error_spec(tripwire, params)
        order = _SEVERITY_ORDER[str(spec.minSeverity)]
        below = [sev for sev, rank in _SEVERITY_ORDER.items() if rank == order - 1]
        assert len(below) == 1
        clean_sev = below[0]
        min_sev = str(spec.minSeverity)
        worse = "CRITICAL"
        assert _SEVERITY_ORDER[worse] > order

        def _mixed(count: int) -> list[str]:
            return [min_sev if i % 2 == 0 else worse for i in range(count)]

        clean = [clean_sev] * spec.windowRuns
        assert error_signature(clean, spec) is False
        single = [min_sev] + [clean_sev] * (spec.windowRuns - 1)
        assert error_signature(single, spec) is False
        tripping = _mixed(spec.threshold) + [clean_sev] * (
            spec.windowRuns - spec.threshold
        )
        assert error_signature(tripping, spec) is True
        older = [min_sev, worse]
        tail_bad = _mixed(spec.threshold - 1)
        tail_good = [clean_sev] * (spec.windowRuns - spec.threshold + 1)
        longer = older + tail_bad + tail_good
        assert len(longer) > spec.windowRuns
        assert error_signature(longer, spec) is False

    service = _reference_services()[0]
    contract = _contract(service)
    tripwire = _tripwire_by_type(contract, "errorSignature")
    env = _envs_for_service(service)[0]
    params = _fleet_params(service, env)
    spec = _error_spec(tripwire, params)
    order = _SEVERITY_ORDER[str(spec.minSeverity)]
    good_sev = next(
        sev for sev, rank in _SEVERITY_ORDER.items() if rank == order - 1
    )
    bad_sev = str(spec.minSeverity)
    worse_sev = "CRITICAL"
    assert _SEVERITY_ORDER[worse_sev] > order
    component = str(tripwire.component)
    comp = next(c for c in contract.components if c.id == component)
    assert comp.logs.container_name == "python"
    run_event = "connector.run.completed"
    if "job" in component:
        run_event = "job.run.completed"
    warn_event = "connector.run.warning"
    if "job" in component:
        warn_event = "job.run.error"
    completed_base = _base_entry(run_event)
    warn_base = _base_entry(warn_event)
    dep_id = f"{service}/{env}"
    tw_id = str(tripwire.id)
    clock = SimClock(T0)
    evaluator = Evaluator(
        [contract],
        [
            Deployment(
                id=dep_id,
                service=service,
                environment=env,
                parameters=_fleet_params(service, env),
            )
        ],
        clock,
    )
    collected: list[TripEvent] = []
    for i in range(spec.windowRuns):
        run = f"err-t5-run-{i:04d}"
        at = T0 + timedelta(seconds=10 * i)
        if i == 0:
            warn = _clone_entry(
                warn_base,
                timestamp=at,
                insert_id=f"err-t5-warn-{i:04d}",
                env=env,
                service=service,
                run_id=run,
                severity=bad_sev,
            )
            clock.set(warn.timestamp)
            collected.extend(evaluator.ingest(warn))
        done = _clone_entry(
            completed_base,
            timestamp=at + timedelta(seconds=1),
            insert_id=f"err-t5-done-{i:04d}",
            env=env,
            service=service,
            run_id=run,
            severity=good_sev,
        )
        clock.set(done.timestamp)
        collected.extend(evaluator.ingest(done))
        collected.extend(evaluator.tick())
    assert [e for e in collected if e.tripwire == tw_id] == []

    clock2 = SimClock(T0)
    evaluator2 = Evaluator(
        [contract],
        [
            Deployment(
                id=dep_id,
                service=service,
                environment=env,
                parameters=_fleet_params(service, env),
            )
        ],
        clock2,
    )
    seen: list[TripEvent] = []
    for i in range(spec.threshold - 1):
        run = f"err-t5-bad-{i:04d}"
        at = T0 + timedelta(seconds=10 * i)
        sev = bad_sev if i % 2 == 0 else worse_sev
        warn = _clone_entry(
            warn_base,
            timestamp=at,
            insert_id=f"err-t5-badw-{i:04d}",
            env=env,
            service=service,
            run_id=run,
            severity=sev,
        )
        clock2.set(warn.timestamp)
        seen.extend(evaluator2.ingest(warn))
        done = _clone_entry(
            completed_base,
            timestamp=at + timedelta(seconds=1),
            insert_id=f"err-t5-badd-{i:04d}",
            env=env,
            service=service,
            run_id=run,
            severity=good_sev,
        )
        clock2.set(done.timestamp)
        seen.extend(evaluator2.ingest(done))
        seen.extend(evaluator2.tick())
    assert [e for e in seen if e.tripwire == tw_id] == []
    run = "err-t5-bad-final"
    at = T0 + timedelta(seconds=10 * spec.threshold)
    warn = _clone_entry(
        warn_base,
        timestamp=at,
        insert_id="err-t5-badw-final",
        env=env,
        service=service,
        run_id=run,
        severity=worse_sev,
    )
    clock2.set(warn.timestamp)
    seen.extend(evaluator2.ingest(warn))
    done = _clone_entry(
        completed_base,
        timestamp=at + timedelta(seconds=1),
        insert_id="err-t5-badd-final",
        env=env,
        service=service,
        run_id=run,
        severity=good_sev,
    )
    clock2.set(done.timestamp)
    seen.extend(evaluator2.ingest(done))
    seen.extend(evaluator2.tick())
    assert (
        len([e for e in seen if e.kind == "trip" and e.tripwire == tw_id]) == 1
    )

    clock3 = SimClock(T0)
    evaluator3 = Evaluator(
        [contract],
        [
            Deployment(
                id=dep_id,
                service=service,
                environment=env,
                parameters=_fleet_params(service, env),
            )
        ],
        clock3,
    )
    multi: list[TripEvent] = []
    shared_run = "err-t5-multi-run-0001"
    for j in range(spec.windowRuns):
        at_j = T0 + timedelta(seconds=10 * j)
        entry_j = _clone_entry(
            warn_base,
            timestamp=at_j,
            insert_id=f"err-t5-multi-w-{j:04d}",
            env=env,
            service=service,
            run_id=shared_run,
            severity=bad_sev,
        )
        clock3.set(entry_j.timestamp)
        multi.extend(evaluator3.ingest(entry_j))
    shared_done = _clone_entry(
        completed_base,
        timestamp=T0 + timedelta(seconds=10 * spec.windowRuns),
        insert_id="err-t5-multi-done",
        env=env,
        service=service,
        run_id=shared_run,
        severity=good_sev,
    )
    clock3.set(shared_done.timestamp)
    multi.extend(evaluator3.ingest(shared_done))
    multi.extend(evaluator3.tick())
    for i in range(spec.windowRuns - 1):
        run = f"err-t5-multi-clean-{i:04d}"
        at = T0 + timedelta(seconds=1000 + 10 * i)
        done = _clone_entry(
            completed_base,
            timestamp=at,
            insert_id=f"err-t5-multi-cd-{i:04d}",
            env=env,
            service=service,
            run_id=run,
            severity=good_sev,
        )
        clock3.set(done.timestamp)
        multi.extend(evaluator3.ingest(done))
        multi.extend(evaluator3.tick())
    assert [e for e in multi if e.tripwire == tw_id] == []


def test_d4_t6_dedupe():
    """D4-T6: no second trip for (deployment, tripwire) before recovery."""
    service = _reference_services()[0]
    contract = _contract(service)
    cadence_tw = _tripwire_by_type(contract, "cadence")
    env = _envs_for_service(service)[0]
    interval_s, max_silent, event = _cadence_params(contract, service, env)
    dep_id = f"{service}/{env}"
    tw_id = str(cadence_tw.id)
    base = _base_entry(event)
    clock = SimClock(T0)
    evaluator = Evaluator(
        [contract],
        [
            Deployment(
                id=dep_id,
                service=service,
                environment=env,
                parameters=_fleet_params(service, env),
            )
        ],
        clock,
    )
    first = _clone_entry(
        base,
        timestamp=T0 + timedelta(seconds=5),
        insert_id="dedupe-heart-0001",
        env=env,
        service=service,
        run_id="dedupe-run-0001",
    )
    clock.set(first.timestamp)
    assert evaluator.ingest(first) == []
    due = T0 + timedelta(seconds=5 + max_silent * interval_s + 1)
    clock.set(due)
    trips = evaluator.tick()
    assert (
        len([e for e in trips if e.kind == "trip" and e.tripwire == tw_id])
        == 1
    )
    for step in range(1, 6):
        clock.set(due + timedelta(seconds=15 * step))
        more = evaluator.tick()
        assert [
            e for e in more if e.kind == "trip" and e.tripwire == tw_id
        ] == []
    assert evaluator.status()[dep_id][tw_id] == "tripped"
    beat = _clone_entry(
        base,
        timestamp=due + timedelta(seconds=90),
        insert_id="dedupe-heart-0002",
        env=env,
        service=service,
        run_id="dedupe-run-0002",
    )
    clock.set(beat.timestamp)
    recovered = evaluator.ingest(beat)
    recovered.extend(evaluator.tick())
    assert (
        len(
            [e for e in recovered if e.kind == "recovery" and e.tripwire == tw_id]
        )
        == 1
    )
    due2 = beat.timestamp + timedelta(seconds=max_silent * interval_s + 1)
    clock.set(due2)
    again = evaluator.tick()
    assert (
        len([e for e in again if e.kind == "trip" and e.tripwire == tw_id]) == 1
    )

    schema_tw: Any = None
    schema_service = ""
    for svc in _reference_services():
        candidate = _contract(svc)
        for tripwire in candidate.tripwires:
            if tripwire.type == "schema":
                schema_tw = tripwire
                schema_service = svc
                break
        if schema_tw is not None:
            break
    assert schema_tw is not None
    schema_spec = _schema_spec(schema_tw)
    assert schema_spec.event is not None
    schema_contract = _contract(schema_service)
    schema_env = _envs_for_service(schema_service)[0]
    schema_dep = f"{schema_service}/{schema_env}"
    schema_tw_id = str(schema_tw.id)
    schema_base = _base_entry(str(schema_spec.event))
    field_path = str(schema_spec.field).removeprefix("jsonPayload.")
    clock_s = SimClock(T0)
    evaluator_s = Evaluator(
        [schema_contract],
        [
            Deployment(
                id=schema_dep,
                service=schema_service,
                environment=schema_env,
                parameters=_fleet_params(schema_service, schema_env),
            )
        ],
        clock_s,
    )

    def _bad(index: int) -> LogEntry:
        entry = _clone_entry(
            schema_base,
            timestamp=T0 + timedelta(seconds=10 * index),
            insert_id=f"dedupe-schema-{index:04d}",
            env=schema_env,
            service=schema_service,
            run_id=f"dedupe-schema-run-{index:04d}",
        )
        payload = entry.jsonPayload
        if schema_spec.where:
            for key, value in schema_spec.where.items():
                _nested_set(
                    payload, key.removeprefix("jsonPayload."), value
                )
        if schema_spec.enum is not None:
            outside = None
            for candidate in (9999, -9999, 0, 1):
                if candidate not in schema_spec.enum:
                    outside = candidate
                    break
            assert outside is not None
            _nested_set(payload, field_path, outside)
        elif schema_spec.minimum is not None:
            _nested_set(payload, field_path, int(schema_spec.minimum) - 1)
        else:
            _nested_del(payload, field_path)
        return entry

    collect: list[TripEvent] = []
    for i in range(schema_spec.consecutive):
        bad_entry = _bad(i)
        clock_s.set(bad_entry.timestamp)
        collect.extend(evaluator_s.ingest(bad_entry))
        collect.extend(evaluator_s.tick())
    assert (
        len(
            [
                e
                for e in collect
                if e.kind == "trip" and e.tripwire == schema_tw_id
            ]
        )
        == 1
    )
    for i in range(schema_spec.consecutive, schema_spec.consecutive + 3):
        bad_entry = _bad(i)
        clock_s.set(bad_entry.timestamp)
        more = evaluator_s.ingest(bad_entry)
        more.extend(evaluator_s.tick())
        assert [
            e for e in more if e.kind == "trip" and e.tripwire == schema_tw_id
        ] == []


def test_d4_t7_recovery():
    """D4-T7: cadence, canary, schema and errorSignature recover correctly."""
    service = _reference_services()[0]
    contract = _contract(service)
    cadence_tw = _tripwire_by_type(contract, "cadence")
    env = _envs_for_service(service)[0]
    interval_s, max_silent, event = _cadence_params(contract, service, env)
    dep_id = f"{service}/{env}"
    cadence_id = str(cadence_tw.id)
    base = _base_entry(event)
    clock = SimClock(T0)
    evaluator = Evaluator(
        [contract],
        [
            Deployment(
                id=dep_id,
                service=service,
                environment=env,
                parameters=_fleet_params(service, env),
            )
        ],
        clock,
    )
    first = _clone_entry(
        base,
        timestamp=T0 + timedelta(seconds=5),
        insert_id="rec-cad-0001",
        env=env,
        service=service,
        run_id="rec-cad-run-0001",
    )
    assert evaluator.ingest(first) == []
    due = T0 + timedelta(seconds=5 + max_silent * interval_s + 1)
    clock.set(due)
    cad_trips = [
        e for e in evaluator.tick() if e.kind == "trip" and e.tripwire == cadence_id
    ]
    assert len(cad_trips) == 1
    beat = _clone_entry(
        base,
        timestamp=due + timedelta(seconds=5),
        insert_id="rec-cad-0002",
        env=env,
        service=service,
        run_id="rec-cad-run-0002",
    )
    clock.set(beat.timestamp)
    recovered = evaluator.ingest(beat)
    recovered.extend(evaluator.tick())
    assert (
        len(
            [
                e
                for e in recovered
                if e.kind == "recovery" and e.tripwire == cadence_id
            ]
        )
        == 1
    )

    synth_service = ""
    synth_contract: Contract | None = None
    for svc in _reference_services():
        candidate = _contract(svc)
        try:
            _synthetic_params(candidate)
        except AssertionError:
            continue
        synth_service = svc
        synth_contract = candidate
        break
    assert synth_contract is not None
    every, deadline_s, synth_tw_id, synth_comp = _synthetic_params(synth_contract)
    assert every >= 1
    synth_env = _envs_for_service(synth_service)[0]
    synth_dep = f"{synth_service}/{synth_env}"
    schema_event = None
    for tripwire in synth_contract.tripwires:
        if tripwire.component == synth_comp and tripwire.type == "schema":
            schema_event = str(tripwire.spec.get("event"))
    assert schema_event is not None
    etl_base = _base_entry(schema_event)
    clock_c = SimClock(T0)
    evaluator_c = Evaluator(
        [synth_contract],
        [
            Deployment(
                id=synth_dep,
                service=synth_service,
                environment=synth_env,
                parameters=_fleet_params(synth_service, synth_env),
            )
        ],
        clock_c,
    )
    provider_old = "canary-t7-old-0001"
    evaluator_c.canary_planted(synth_dep, provider_old, T0)
    clock_c.set(T0 + timedelta(seconds=deadline_s + 1))
    assert (
        len(
            [
                e
                for e in evaluator_c.tick()
                if e.kind == "trip" and e.tripwire == synth_tw_id
            ]
        )
        == 1
    )
    late = _clone_entry(
        etl_base,
        timestamp=T0 + timedelta(seconds=deadline_s + 2),
        insert_id="rec-canary-late-0001",
        env=synth_env,
        service=synth_service,
    )
    late.jsonPayload["alert"]["canary"] = True
    late.jsonPayload["alert"]["ticket_id"] = provider_old
    clock_c.set(late.timestamp)
    late_out = evaluator_c.ingest(late)
    late_out.extend(evaluator_c.tick())
    assert [
        e
        for e in late_out
        if e.kind == "recovery" and e.tripwire == synth_tw_id
    ] == []
    assert evaluator_c.status()[synth_dep][synth_tw_id] == "tripped"
    provider_new = "canary-t7-new-0002"
    planted_new_at = clock_c.now()
    evaluator_c.canary_planted(synth_dep, provider_new, planted_new_at)
    seen = _clone_entry(
        etl_base,
        timestamp=planted_new_at + timedelta(seconds=deadline_s - 1),
        insert_id="rec-canary-seen-0002",
        env=synth_env,
        service=synth_service,
    )
    seen.jsonPayload["alert"]["canary"] = True
    seen.jsonPayload["alert"]["ticket_id"] = provider_new
    clock_c.set(seen.timestamp)
    seen_out = evaluator_c.ingest(seen)
    seen_out.extend(evaluator_c.tick())
    assert (
        len(
            [
                e
                for e in seen_out
                if e.kind == "recovery" and e.tripwire == synth_tw_id
            ]
        )
        == 1
    )

    hollow_service = ""
    hollow_tw: Any = None
    for svc in _reference_services():
        candidate = _contract(svc)
        for tripwire in candidate.tripwires:
            if tripwire.type == "schema" and tripwire.spec.get("where"):
                hollow_service = svc
                hollow_tw = tripwire
    assert hollow_tw is not None
    hollow_contract = _contract(hollow_service)
    hollow_spec = _schema_spec(hollow_tw)
    hollow_env = _envs_for_service(hollow_service)[0]
    hollow_dep = f"{hollow_service}/{hollow_env}"
    hollow_id = str(hollow_tw.id)
    assert hollow_spec.event is not None
    assert hollow_spec.recoverAfter is not None
    assert hollow_spec.minimum is not None
    job_base = _base_entry(str(hollow_spec.event))

    def _job(
        index: int, *, status: str, synced: Any, tag: str
    ) -> LogEntry:
        entry = _clone_entry(
            job_base,
            timestamp=T0 + timedelta(seconds=10 * index),
            insert_id=f"rec-hollow-{tag}-{index:04d}",
            env=hollow_env,
            service=hollow_service,
            run_id=f"rec-hollow-{tag}-run-{index:04d}",
        )
        entry.jsonPayload["status"] = status
        entry.jsonPayload["indicatorsSynced"] = synced
        return entry

    clock_h = SimClock(T0)
    evaluator_h = Evaluator(
        [hollow_contract],
        [
            Deployment(
                id=hollow_dep,
                service=hollow_service,
                environment=hollow_env,
                parameters=_fleet_params(hollow_service, hollow_env),
            )
        ],
        clock_h,
    )
    for i in range(hollow_spec.consecutive):
        entry = _job(i, status="SUCCESS", synced=0, tag="trip")
        clock_h.set(entry.timestamp)
        out = evaluator_h.ingest(entry)
        out.extend(evaluator_h.tick())
    assert evaluator_h.status()[hollow_dep][hollow_id] == "tripped"
    valid_synced = int(hollow_spec.minimum) + 3
    recover_after = int(hollow_spec.recoverAfter)
    valid_seen = 0
    first_valid = _job(100, status="SUCCESS", synced=valid_synced, tag="rec")
    clock_h.set(first_valid.timestamp)
    out = evaluator_h.ingest(first_valid)
    out.extend(evaluator_h.tick())
    valid_seen += 1
    assert [
        e for e in out if e.kind == "recovery" and e.tripwire == hollow_id
    ] == []
    err_between = _job(101, status="ERROR", synced=0, tag="rec")
    clock_h.set(err_between.timestamp)
    out = evaluator_h.ingest(err_between)
    out.extend(evaluator_h.tick())
    assert [
        e for e in out if e.kind == "recovery" and e.tripwire == hollow_id
    ] == []
    for i in range(1, recover_after):
        entry = _job(102 + i, status="SUCCESS", synced=valid_synced, tag="rec")
        clock_h.set(entry.timestamp)
        out = evaluator_h.ingest(entry)
        out.extend(evaluator_h.tick())
        valid_seen += 1
        if valid_seen < recover_after:
            assert [
                e
                for e in out
                if e.kind == "recovery" and e.tripwire == hollow_id
            ] == []
        else:
            assert (
                len(
                    [
                        e
                        for e in out
                        if e.kind == "recovery" and e.tripwire == hollow_id
                    ]
                )
                == 1
            )

    dumped = hollow_contract.model_dump()
    for item in dumped["tripwires"]:
        if item["id"] == hollow_id:
            item["spec"].pop("recoverAfter", None)
    default_contract = Contract.model_validate(dumped)
    clock_d = SimClock(T0)
    evaluator_d = Evaluator(
        [default_contract],
        [
            Deployment(
                id=hollow_dep,
                service=hollow_service,
                environment=hollow_env,
                parameters=_fleet_params(hollow_service, hollow_env),
            )
        ],
        clock_d,
    )
    for i in range(hollow_spec.consecutive):
        entry = _job(300 + i, status="SUCCESS", synced=0, tag="def")
        clock_d.set(entry.timestamp)
        out = evaluator_d.ingest(entry)
        out.extend(evaluator_d.tick())
    assert evaluator_d.status()[hollow_dep][hollow_id] == "tripped"
    def_valid_seen = 0
    def_first = _job(400, status="SUCCESS", synced=valid_synced, tag="def")
    clock_d.set(def_first.timestamp)
    out = evaluator_d.ingest(def_first)
    out.extend(evaluator_d.tick())
    def_valid_seen += 1
    assert [
        e for e in out if e.kind == "recovery" and e.tripwire == hollow_id
    ] == []
    def_err = _job(401, status="ERROR", synced=0, tag="def")
    clock_d.set(def_err.timestamp)
    out = evaluator_d.ingest(def_err)
    out.extend(evaluator_d.tick())
    assert [
        e for e in out if e.kind == "recovery" and e.tripwire == hollow_id
    ] == []
    for i in range(1, hollow_spec.consecutive):
        entry = _job(402 + i, status="SUCCESS", synced=valid_synced, tag="def")
        clock_d.set(entry.timestamp)
        out = evaluator_d.ingest(entry)
        out.extend(evaluator_d.tick())
        def_valid_seen += 1
        if def_valid_seen < hollow_spec.consecutive:
            assert [
                e
                for e in out
                if e.kind == "recovery" and e.tripwire == hollow_id
            ] == []
        else:
            assert (
                len(
                    [
                        e
                        for e in out
                        if e.kind == "recovery" and e.tripwire == hollow_id
                    ]
                )
                == 1
            )

    err_service = ""
    err_tw: Any = None
    for svc in _reference_services():
        candidate = _contract(svc)
        for tripwire in candidate.tripwires:
            if tripwire.type == "errorSignature":
                err_service = svc
                err_tw = tripwire
                break
        if err_tw is not None:
            break
    assert err_tw is not None
    err_contract = _contract(err_service)
    err_env = _envs_for_service(err_service)[0]
    err_params = _fleet_params(err_service, err_env)
    err_spec = _error_spec(err_tw, err_params)
    err_dep = f"{err_service}/{err_env}"
    err_id = str(err_tw.id)
    err_comp = str(err_tw.component)
    err_run_event = "connector.run.completed"
    err_warn_event = "connector.run.warning"
    if "job" in err_comp:
        err_run_event = "job.run.completed"
        err_warn_event = "job.run.error"
    err_done_base = _base_entry(err_run_event)
    err_warn_base = _base_entry(err_warn_event)
    order = _SEVERITY_ORDER[str(err_spec.minSeverity)]
    err_good = next(sev for sev, rank in _SEVERITY_ORDER.items() if rank < order)
    err_bad = str(err_spec.minSeverity)
    clock_e = SimClock(T0)
    evaluator_e = Evaluator(
        [err_contract],
        [
            Deployment(
                id=err_dep,
                service=err_service,
                environment=err_env,
                parameters=_fleet_params(err_service, err_env),
            )
        ],
        clock_e,
    )

    def _run(index: int, *, bad: bool, tag: str) -> list[LogEntry]:
        run = f"rec-err-{tag}-{index:04d}"
        at = T0 + timedelta(seconds=10 * index)
        entries: list[LogEntry] = []
        if bad:
            entries.append(
                _clone_entry(
                    err_warn_base,
                    timestamp=at,
                    insert_id=f"rec-err-{tag}-w-{index:04d}",
                    env=err_env,
                    service=err_service,
                    run_id=run,
                    severity=err_bad,
                )
            )
        entries.append(
            _clone_entry(
                err_done_base,
                timestamp=at + timedelta(seconds=1),
                insert_id=f"rec-err-{tag}-d-{index:04d}",
                env=err_env,
                service=err_service,
                run_id=run,
                severity=err_good if not bad else err_good,
            )
        )
        return entries

    for i in range(err_spec.threshold):
        for entry in _run(i, bad=True, tag="bad"):
            clock_e.set(entry.timestamp)
            out = evaluator_e.ingest(entry)
            out.extend(evaluator_e.tick())
    assert evaluator_e.status()[err_dep][err_id] == "tripped"
    recover_k = err_spec.windowRuns - err_spec.threshold + 1
    assert recover_k >= 1
    for j in range(1, recover_k + 2):
        clean_entries = _run(100 + j, bad=False, tag="good")
        rec_out: list[TripEvent] = []
        for entry in clean_entries:
            clock_e.set(entry.timestamp)
            rec_out.extend(evaluator_e.ingest(entry))
        rec_out.extend(evaluator_e.tick())
        recs = [
            e for e in rec_out if e.kind == "recovery" and e.tripwire == err_id
        ]
        if j < recover_k:
            assert recs == []
        elif j == recover_k:
            assert len(recs) == 1
        else:
            assert recs == []

    stall_env = _envs_for_service(synth_service)[0]
    stall_dep = f"{synth_service}/{stall_env}"
    stall_clock = SimClock(T0)
    stall_sink = MemorySink()
    stall_world = World(_fleet(), 77, stall_clock, stall_sink)
    stall_evaluator = Evaluator(
        [synth_contract],
        [
            Deployment(
                id=stall_dep,
                service=synth_service,
                environment=stall_env,
                parameters=_fleet_params(synth_service, stall_env),
            )
        ],
        stall_clock,
    )
    stall_interval_s = int(
        _fleet_params(synth_service, stall_env)["intervalSeconds"]
    )
    fault_id = stall_world.inject(
        FaultSpec(type="connector_stall", environment=stall_env)
    )
    planted = stall_world.plant_canary(stall_env)
    stall_evaluator.canary_planted(stall_dep, planted, stall_clock.now())
    consumed = 0
    deadline_tick = T0 + timedelta(seconds=deadline_s + 1)
    stall_world.step_until(deadline_tick)
    for entry in stall_sink.entries[consumed:]:
        stall_evaluator.ingest(entry)
    consumed = len(stall_sink.entries)
    stall_trips = stall_evaluator.tick()
    assert (
        len(
            [
                e
                for e in stall_trips
                if e.kind == "trip" and e.tripwire == synth_tw_id
            ]
        )
        == 1
    )
    stall_world.clear(fault_id)
    cleared_at = stall_clock.now()

    def _canary_seen(provider_id: str) -> bool:
        for entry in stall_sink.entries:
            payload = entry.jsonPayload
            if payload.get("event") != "alert.ingested":
                continue
            alert = payload.get("alert", {})
            if (
                isinstance(alert, dict)
                and alert.get("canary") is True
                and alert.get("ticket_id") == provider_id
            ):
                return True
        return False

    tick_step = 15
    bound_first = cleared_at + timedelta(
        seconds=2 * stall_interval_s + tick_step
    )
    late_outcome: list[TripEvent] = []
    cursor = stall_clock.now()
    while not _canary_seen(planted):
        cursor = cursor + timedelta(seconds=tick_step)
        assert cursor <= bound_first, "late canary never arrived"
        stall_world.step_until(cursor)
        for entry in stall_sink.entries[consumed:]:
            late_outcome.extend(stall_evaluator.ingest(entry))
        consumed = len(stall_sink.entries)
        late_outcome.extend(stall_evaluator.tick())
    assert _canary_seen(planted)
    assert [
        e
        for e in late_outcome
        if e.kind == "recovery" and e.tripwire == synth_tw_id
    ] == []
    assert stall_evaluator.status()[stall_dep][synth_tw_id] == "tripped"
    planted2 = stall_world.plant_canary(stall_env)
    stall_evaluator.canary_planted(stall_dep, planted2, stall_clock.now())
    bound_second = stall_clock.now() + timedelta(seconds=deadline_s)
    final_out: list[TripEvent] = []
    cursor = stall_clock.now()
    while not _canary_seen(planted2):
        cursor = cursor + timedelta(seconds=tick_step)
        assert cursor <= bound_second, "second canary never arrived"
        stall_world.step_until(cursor)
        for entry in stall_sink.entries[consumed:]:
            final_out.extend(stall_evaluator.ingest(entry))
        consumed = len(stall_sink.entries)
        final_out.extend(stall_evaluator.tick())
    assert (
        len(
            [
                e
                for e in final_out
                if e.kind == "recovery" and e.tripwire == synth_tw_id
            ]
        )
        == 1
    )


def test_d4_t8_isolation():
    """D4-T8: a fault never trips another environment's tripwires."""
    all_scenarios: list[Scenario] = []
    for path in sorted((ROOT / "fixtures" / "scenarios").rglob("*.yaml")):
        all_scenarios.append(load_scenario(path))
    fault_services: dict[str, set[str]] = {}
    for scenario in all_scenarios:
        for fault in scenario.faults:
            fault_services.setdefault(fault.type, set()).add(scenario.service)
    fault_types = sorted(fault_services)
    assert len(fault_types) >= 7
    rng = random.Random(12345)
    tripped: dict[str, bool] = {fault: False for fault in fault_types}
    drawn: set[str] = set()
    for i in range(50):
        fault_type = rng.choice(fault_types)
        drawn.add(fault_type)
        services = sorted(fault_services[fault_type])
        service = rng.choice(services)
        envs = _envs_for_service(service)
        fault_env = rng.choice(envs)
        inject_at = rng.randrange(0, 1800)
        params: dict[str, Any] = {}
        if fault_type == "connector_flap":
            params = {"rate": 0.7}
        elif fault_type == "severity_drift":
            params = {"rate": 0.5}
        contract = _contract(service)
        scenario = Scenario.model_validate(
            {
                "id": f"iso-{i:02d}",
                "service": service,
                "seed": 2000 + i,
                "durationSeconds": 3600,
                "deployments": envs,
                "faults": [
                    {
                        "at": inject_at,
                        "type": fault_type,
                        "environment": fault_env,
                        "params": params,
                    }
                ],
                "expected": {"trips": [], "recoveries": [], "mustNotTrip": []},
                "holdout": False,
            }
        )
        result = run_scenario(contract, scenario)
        assert isinstance(result, ScenarioResult)
        faulted_dep = f"{service}/{fault_env}"
        for event in result.actual:
            assert isinstance(event, TripEvent)
            assert event.deployment == faulted_dep
            if event.kind == "trip":
                tripped[fault_type] = True
    for fault_type in sorted(drawn):
        if fault_type == "connector_flap":
            continue
        assert tripped[fault_type], f"no trip for {fault_type} in 50 runs"


def test_d4_t9_purity_replay(monkeypatch):
    """D4-T9: tripwires are pure and replay is deterministic."""
    parser = configparser.ConfigParser()
    parser.read(ROOT / ".importlinter")
    forbidden_found: set[str] = set()
    for section in parser.sections():
        if not section.startswith("importlinter:contract:"):
            continue
        sources = parser.get(section, "source_modules", fallback="")
        if "fleetsre.evaluator.tripwires" not in sources:
            continue
        raw = parser.get(section, "forbidden_modules", fallback="")
        for line in raw.splitlines():
            name = line.strip()
            if name:
                forbidden_found.add(name)
    for module in (
        "fleetsre.adapters",
        "fleetsre.mock",
        "socket",
        "httpx",
        "urllib",
        "google",
    ):
        assert module in forbidden_found
    proc = subprocess.run(
        ["lint-imports"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    pure_service = _reference_services()[0]
    pure_contract = _contract(pure_service)
    pure_env = _envs_for_service(pure_service)[0]
    interval_s, max_silent, _event = _cadence_params(
        pure_contract, pure_service, pure_env
    )
    schema_tw = next(
        t for t in pure_contract.tripwires if t.type == "schema"
    )
    pure_schema = _schema_spec(schema_tw)
    assert pure_schema.event is not None
    pure_entry = _base_entry(str(pure_schema.event))
    err_tw = _tripwire_by_type(pure_contract, "errorSignature")
    pure_err = _error_spec(err_tw, _fleet_params(pure_service, pure_env))
    synth_deadline = None
    for svc in _reference_services():
        try:
            _every, deadline, _tid, _comp = _synthetic_params(_contract(svc))
        except AssertionError:
            continue
        synth_deadline = deadline
        break
    assert synth_deadline is not None

    def _forbidden_open(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("I/O forbidden in tripwires")

    def _forbidden_socket(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("I/O forbidden in tripwires")

    monkeypatch.setattr(builtins, "open", _forbidden_open)
    monkeypatch.setattr(socket, "socket", _forbidden_socket)
    armed = T0
    last = T0 + timedelta(seconds=interval_s)
    now = last + timedelta(seconds=max_silent * interval_s)
    assert isinstance(
        cadence_due(last, armed, now, interval_s, max_silent), bool
    )
    assert schema_violation(pure_entry, pure_schema) is None or isinstance(
        schema_violation(pure_entry, pure_schema), str
    )
    assert isinstance(
        error_signature(["INFO"] * pure_err.windowRuns, pure_err), bool
    )
    assert isinstance(
        canary_missed(T0, None, T0 + timedelta(seconds=1), synth_deadline),
        bool,
    )
    monkeypatch.undo()

    stall_matches = sorted((ROOT / "fixtures" / "scenarios").rglob("stall.yaml"))
    assert len(stall_matches) >= 1
    det_scenario = load_scenario(stall_matches[0])
    det_contract = _contract(det_scenario.service)
    first = run_scenario(det_contract, det_scenario)
    second = run_scenario(det_contract, det_scenario)
    assert len(first.actual) > 0
    assert first.actual == second.actual


def test_d4_t10_out_of_order():
    """D4-T10: arrivals displaced by up to 60s cause no false trips."""
    displacement = 60
    duration = 3600
    tick_seconds = 15
    lag = 50
    horizon = 500
    for service in _reference_services():
        contract = _contract(service)
        try:
            cadence_tw = _tripwire_by_type(contract, "cadence")
        except AssertionError:
            continue
        env = _envs_for_service(service)[0]
        interval_s, max_silent, event = _cadence_params(contract, service, env)
        window = max_silent * interval_s
        dep_id = f"{service}/{env}"
        tw_id = str(cadence_tw.id)
        base = _base_entry(event)
        det_clock = SimClock(T0)
        det_evaluator = Evaluator(
            [contract],
            [
                Deployment(
                    id=dep_id,
                    service=service,
                    environment=env,
                    parameters=_fleet_params(service, env),
                )
            ],
            det_clock,
        )
        newer = _clone_entry(
            base,
            timestamp=T0 + timedelta(seconds=horizon),
            insert_id="ooo-det-newer-0001",
            env=env,
            service=service,
            run_id="ooo-det-run-newer",
        )
        older = _clone_entry(
            base,
            timestamp=T0 + timedelta(seconds=horizon - lag),
            insert_id="ooo-det-older-0002",
            env=env,
            service=service,
            run_id="ooo-det-run-older",
        )
        assert det_evaluator.ingest(newer) == []
        assert det_evaluator.ingest(older) == []
        early = T0 + timedelta(seconds=horizon - lag + window + 1)
        det_clock.set(early)
        assert [
            e
            for e in det_evaluator.tick()
            if e.kind == "trip" and e.tripwire == tw_id
        ] == []
        late = T0 + timedelta(seconds=horizon + window + 1)
        det_clock.set(late)
        assert (
            len(
                [
                    e
                    for e in det_evaluator.tick()
                    if e.kind == "trip" and e.tripwire == tw_id
                ]
            )
            == 1
        )
    for service in _reference_services():
        contract = _contract(service)
        try:
            _tripwire_by_type(contract, "cadence")
        except AssertionError:
            continue
        deployments = _deployments_for_service(service)
        assert deployments
        gen_clock = SimClock(T0)
        sink = MemorySink()
        world = World(_fleet(), 999, gen_clock, sink)
        world.step_until(T0 + timedelta(seconds=duration))
        entries = list(sink.entries)
        assert len(entries) > 0
        rng = random.Random(2024)
        arrivals: list[tuple[datetime, int, LogEntry]] = []
        for index, entry in enumerate(entries):
            delay = rng.random() * displacement
            arrivals.append(
                (entry.timestamp + timedelta(seconds=delay), index, entry)
            )
        arrivals.sort(key=lambda item: (item[0], item[1]))
        arrival_entries = [entry for _at, _idx, entry in arrivals]
        stamp_order = sorted(arrival_entries, key=lambda e: e.timestamp)
        assert arrival_entries != stamp_order
        inverted = any(
            arrival_entries[i].timestamp > arrival_entries[i + 1].timestamp
            for i in range(len(arrival_entries) - 1)
        )
        assert inverted
        clock = SimClock(T0)
        evaluator = Evaluator([contract], deployments, clock)
        collected: list[TripEvent] = []
        position = 0
        end = T0 + timedelta(seconds=duration + displacement)
        steps = int((duration + displacement) // tick_seconds) + 1
        for step in range(steps):
            tick_at = T0 + timedelta(seconds=step * tick_seconds)
            if tick_at > end:
                break
            clock.set(tick_at)
            while position < len(arrivals) and arrivals[position][0] <= tick_at:
                _arrival, _index, entry = arrivals[position]
                collected.extend(evaluator.ingest(entry))
                position += 1
            collected.extend(evaluator.tick())
        while position < len(arrivals):
            _arrival, _index, entry = arrivals[position]
            collected.extend(evaluator.ingest(entry))
            position += 1
        collected.extend(evaluator.tick())
        assert collected == []


def test_d4_t11_golden_fixtures():
    """D4-T11: golden streams replay to their expected events."""
    expected_paths = sorted((ROOT / "fixtures" / "golden").glob("*.expected.json"))
    assert len(expected_paths) >= 4
    for expected_path in expected_paths:
        expected = json.loads(expected_path.read_text())
        service = str(expected["service"])
        dep_ids = list(expected["deployments"])
        raw_t0 = str(expected["t0"])
        t0 = datetime.fromisoformat(raw_t0.replace("Z", "+00:00"))
        duration_s = int(expected["durationSeconds"])
        tick_s = int(expected["tickSeconds"])
        plantings = list(expected.get("canariesPlanted", []))
        wanted = list(expected["events"])
        jsonl_name = expected_path.name.replace(".expected.json", ".jsonl")
        entries = read_jsonl(ROOT / "fixtures" / "golden" / jsonl_name)
        contract = _contract(service)
        deployments = _deployments_for_ids(service, dep_ids)
        clock = SimClock(t0)
        evaluator = Evaluator([contract], deployments, clock)
        planted: set[str] = set()
        collected: list[TripEvent] = []
        consumed = 0
        ordered = list(entries)
        steps = duration_s // tick_s + 2
        for step in range(steps):
            tick_at = t0 + timedelta(seconds=step * tick_s)
            if tick_at > t0 + timedelta(seconds=duration_s):
                break
            clock.set(tick_at)
            for item in plantings:
                key = f"{item['deployment']}/{item['providerIncidentId']}"
                at = t0 + timedelta(seconds=int(item["atOffsetSeconds"]))
                if key not in planted and at <= tick_at:
                    evaluator.canary_planted(
                        str(item["deployment"]),
                        str(item["providerIncidentId"]),
                        at,
                    )
                    planted.add(key)
            while consumed < len(ordered) and ordered[consumed].timestamp <= tick_at:
                collected.extend(evaluator.ingest(ordered[consumed]))
                consumed += 1
            collected.extend(evaluator.tick())
        assert len(collected) == len(wanted), expected_path.name
        used = [False] * len(collected)
        for want in wanted:
            matched = False
            for index, actual in enumerate(collected):
                if used[index]:
                    continue
                assert isinstance(actual, TripEvent)
                if (
                    actual.kind != want["kind"]
                    or actual.deployment != want["deployment"]
                    or actual.tripwire != want["tripwire"]
                ):
                    continue
                offset = (actual.at - t0).total_seconds()
                if abs(offset - float(want["atOffsetSeconds"])) > tick_s:
                    continue
                used[index] = True
                matched = True
                break
            assert matched, f"{expected_path.name}: no match for {want}"
        assert all(used), expected_path.name


def test_d4_t12_reference_scorecard(
    _ref_contracts: dict[str, Contract],
    _ref_scenarios: dict[str, list[Scenario]],
    _ref_scorecards: dict[str, Scorecard],
):
    """D4-T12: reference contracts pass their full libraries cleanly."""
    assert set(_ref_scorecards) == set(_ref_contracts)
    for service, board in _ref_scorecards.items():
        assert isinstance(board, Scorecard)
        scenarios = _ref_scenarios[service]
        assert len(board.results) == len(scenarios)
        assert board.passed is True
        assert board.expected_hit_rate == 1.0
        assert board.false_positives == 0
        by_id = {scenario.id: scenario for scenario in scenarios}
        for result in board.results:
            assert isinstance(result, ScenarioResult)
            assert result.scenario_id in by_id
            assert result.missing == []
            assert result.unexpected == []
            assert result.passed is True


def test_d4_t13_mutants_fail(
    _mutant_cases: list[tuple[str, Contract, list[Scenario]]],
    _mutant_scorecards: list[tuple[str, Scorecard]],
):
    """D4-T13: every weakened mutant contract fails its scorecard."""
    assert len(_mutant_cases) >= 1
    assert len(_mutant_scorecards) == len(_mutant_cases)
    for (name, _contract, _scenarios), (_seen, board) in zip(
        _mutant_cases, _mutant_scorecards
    ):
        assert name == _seen
        assert isinstance(board, Scorecard)
        assert board.passed is False, name
        assert board.expected_hit_rate < 1.0, name


def test_d4_t14_latency_reporting(
    _ref_contracts: dict[str, Contract],
    _ref_scenarios: dict[str, list[Scenario]],
    _ref_scorecards: dict[str, Scorecard],
):
    """D4-T14: every expected trip reports latency within its budget."""
    for service, board in _ref_scorecards.items():
        assert board.passed is True
        by_id = {scenario.id: scenario for scenario in _ref_scenarios[service]}
        for result in board.results:
            scenario = by_id[result.scenario_id]
            for trip in scenario.expected.trips:
                dep_id = f"{service}/{trip.deployment}"
                key = ("trip", dep_id, trip.tripwire)
                assert key in result.latencies
                assert result.latencies[key] >= 0
                assert result.latencies[key] <= trip.withinSeconds
            for recovery in scenario.expected.recoveries:
                dep_id = f"{service}/{recovery.deployment}"
                key = ("recovery", dep_id, recovery.tripwire)
                assert key in result.latencies
                assert result.latencies[key] >= 0
                assert result.latencies[key] <= recovery.withinSeconds


def test_d4_t15_change_point():
    """D4-T15: change points re-arm cadence and revert after duration."""
    target_service = ""
    target_tw: Any = None
    for svc in _reference_services():
        candidate = _contract(svc)
        for tripwire in candidate.tripwires:
            if (
                tripwire.type == "cadence"
                and tripwire.id in candidate.changePoint.overrides
            ):
                target_service = svc
                target_tw = tripwire
                break
        if target_tw is not None:
            break
    assert target_tw is not None
    contract = _contract(target_service)
    env = _envs_for_service(target_service)[0]
    interval_s, orig_max, event = _cadence_params(contract, target_service, env)
    tw_id = str(target_tw.id)
    component = str(target_tw.component)
    overrides = dict(contract.changePoint.overrides[tw_id])
    assert "maxSilentIntervals" in overrides
    override_max = int(overrides["maxSilentIntervals"])
    assert override_max != orig_max
    duration_s = int(contract.changePoint.durationSeconds)
    dep_id = f"{target_service}/{env}"
    base = _base_entry(event)
    clock = SimClock(T0)
    evaluator = Evaluator(
        [contract],
        [
            Deployment(
                id=dep_id,
                service=target_service,
                environment=env,
                parameters=_fleet_params(target_service, env),
            )
        ],
        clock,
    )
    old_at = T0 + timedelta(seconds=5)
    old = _clone_entry(
        base,
        timestamp=old_at,
        insert_id="cp-old-heart-0001",
        env=env,
        service=target_service,
        run_id="cp-old-run-0001",
    )
    assert evaluator.ingest(old) == []
    entry_at = T0 + timedelta(seconds=10)
    clock.set(entry_at)
    evaluator.enter_change_point(
        dep_id, component, duration_s=duration_s, overrides={tw_id: dict(overrides)}
    )
    old_due = old_at + timedelta(seconds=override_max * interval_s + 1)
    clock.set(old_due)
    assert [
        e for e in evaluator.tick() if e.kind == "trip" and e.tripwire == tw_id
    ] == []
    entry_due = entry_at + timedelta(seconds=override_max * interval_s + 1)
    clock.set(entry_due)
    trips = evaluator.tick()
    assert (
        len([e for e in trips if e.kind == "trip" and e.tripwire == tw_id])
        == 1
    )
    beat_at = entry_due + timedelta(seconds=5)
    beat = _clone_entry(
        base,
        timestamp=beat_at,
        insert_id="cp-beat-0002",
        env=env,
        service=target_service,
        run_id="cp-beat-run-0002",
    )
    clock.set(beat_at)
    rec = evaluator.ingest(beat)
    rec.extend(evaluator.tick())
    assert (
        len(
            [e for e in rec if e.kind == "recovery" and e.tripwire == tw_id]
        )
        == 1
    )
    expiry = entry_at + timedelta(seconds=duration_s)
    fresh_at = expiry + timedelta(seconds=5)
    fresh = _clone_entry(
        base,
        timestamp=fresh_at,
        insert_id="cp-fresh-0003",
        env=env,
        service=target_service,
        run_id="cp-fresh-run-0003",
    )
    clock.set(fresh_at)
    assert evaluator.ingest(fresh) == []
    short_due = fresh_at + timedelta(seconds=override_max * interval_s + 1)
    clock.set(short_due)
    assert [
        e for e in evaluator.tick() if e.kind == "trip" and e.tripwire == tw_id
    ] == []
    long_due = fresh_at + timedelta(seconds=orig_max * interval_s + 1)
    clock.set(long_due)
    assert (
        len(
            [
                e
                for e in evaluator.tick()
                if e.kind == "trip" and e.tripwire == tw_id
            ]
        )
        == 1
    )


def test_d4_t16_canary_scheduler():
    """D4-T16: scheduler plants every everyMinutes via World.plant_canary."""
    synth_service = ""
    synth_contract: Contract | None = None
    plain_service = ""
    plain_contract: Contract | None = None
    for svc in _reference_services():
        candidate = _contract(svc)
        try:
            _synthetic_params(candidate)
        except AssertionError:
            if plain_contract is None:
                plain_service = svc
                plain_contract = candidate
            continue
        if synth_contract is None:
            synth_service = svc
            synth_contract = candidate
    assert synth_contract is not None
    assert plain_contract is not None
    every, _deadline, synth_tw_id, _comp = _synthetic_params(synth_contract)
    assert synth_tw_id
    synth_envs = _envs_for_service(synth_service)
    plain_envs = _envs_for_service(plain_service)
    contracts = [synth_contract, plain_contract]
    deployments = _deployments_for_service(synth_service) + _deployments_for_service(
        plain_service
    )
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(_fleet(), 555, clock, sink)
    evaluator = Evaluator(contracts, deployments, clock)
    plant_calls: list[tuple[str, datetime, str]] = []
    orig_plant = world.plant_canary

    def _spy_plant(environment: str) -> str:
        provider_id = orig_plant(environment)
        plant_calls.append((environment, clock.now(), provider_id))
        return provider_id

    world.plant_canary = _spy_plant  # type: ignore[method-assign]
    planted_calls: list[tuple[str, str, datetime]] = []
    orig_planted = evaluator.canary_planted

    def _spy_planted(
        deployment: str, provider_incident_id: str, at: datetime
    ) -> None:
        planted_calls.append((deployment, provider_incident_id, at))
        return orig_planted(deployment, provider_incident_id, at)

    evaluator.canary_planted = _spy_planted  # type: ignore[method-assign]
    scheduler = CanaryScheduler(world, evaluator, contracts, clock)
    window_s = every * 60 * 2 + every * 60 // 2
    assert window_s % (every * 60) != 0
    tick_seconds = 15
    steps = window_s // tick_seconds + 1
    for step in range(steps):
        tick_at = T0 + timedelta(seconds=step * tick_seconds)
        if tick_at > T0 + timedelta(seconds=window_s):
            break
        clock.set(tick_at)
        scheduler.tick()
    per_dep = window_s // (every * 60)
    assert per_dep == 2
    assert len(plant_calls) == per_dep * len(synth_envs)
    first_at = T0 + timedelta(seconds=every * 60)
    assert min(at for _env, at, _pid in plant_calls) == first_at
    for _env, at, _pid in plant_calls:
        elapsed = int((at - T0).total_seconds())
        assert elapsed % (every * 60) == 0
    assert len(planted_calls) == len(plant_calls)
    by_provider = {pid: (env, at) for env, at, pid in plant_calls}
    for deployment, provider_id, at in planted_calls:
        assert provider_id in by_provider
        env, planted_at = by_provider[provider_id]
        assert deployment == f"{synth_service}/{env}"
        assert at == planted_at
        assert deployment.split("/", 1)[0] == synth_service
    assert all(env in synth_envs for env, _at, _pid in plant_calls)
    assert len(plain_envs) >= 1


def test_d4_t17_trip_evidence():
    """D4-T17: trips carry heartbeat, violations, warnings or canary ID."""
    cad_service = _reference_services()[0]
    cad_contract = _contract(cad_service)
    cad_tw = _tripwire_by_type(cad_contract, "cadence")
    cad_env = _envs_for_service(cad_service)[0]
    interval_s, max_silent, event = _cadence_params(
        cad_contract, cad_service, cad_env
    )
    cad_dep = f"{cad_service}/{cad_env}"
    cad_id = str(cad_tw.id)
    cad_base = _base_entry(event)
    cad_clock = SimClock(T0)
    cad_evaluator = Evaluator(
        [cad_contract],
        [
            Deployment(
                id=cad_dep,
                service=cad_service,
                environment=cad_env,
                parameters=_fleet_params(cad_service, cad_env),
            )
        ],
        cad_clock,
    )
    heart = _clone_entry(
        cad_base,
        timestamp=T0 + timedelta(seconds=5),
        insert_id="ev-cad-heart-0001",
        env=cad_env,
        service=cad_service,
        run_id="ev-cad-run-0001",
    )
    cad_clock.set(heart.timestamp)
    assert cad_evaluator.ingest(heart) == []
    cad_clock.set(T0 + timedelta(seconds=5 + max_silent * interval_s + 1))
    cad_trips = [
        e for e in cad_evaluator.tick() if e.kind == "trip" and e.tripwire == cad_id
    ]
    assert len(cad_trips) == 1
    assert "ev-cad-heart-0001" in cad_trips[0].evidence

    cp_overrides = cad_contract.changePoint.overrides.get(cad_id)
    if cp_overrides is not None:
        cp_clock = SimClock(T0)
        cp_evaluator = Evaluator(
            [cad_contract],
            [
                Deployment(
                    id=cad_dep,
                    service=cad_service,
                    environment=cad_env,
                    parameters=_fleet_params(cad_service, cad_env),
                )
            ],
            cp_clock,
        )
        pre = _clone_entry(
            cad_base,
            timestamp=T0 + timedelta(seconds=5),
            insert_id="ev-cad-pre-cp-0001",
            env=cad_env,
            service=cad_service,
            run_id="ev-cad-pre-cp-run",
        )
        cp_clock.set(pre.timestamp)
        assert cp_evaluator.ingest(pre) == []
        cp_clock.set(T0 + timedelta(seconds=10))
        cp_component = str(cad_tw.component)
        cp_duration = int(cad_contract.changePoint.durationSeconds)
        cp_evaluator.enter_change_point(
            cad_dep,
            cp_component,
            duration_s=cp_duration,
            overrides={cad_id: dict(cp_overrides)},
        )
        override_max = int(cp_overrides["maxSilentIntervals"])
        cp_due = T0 + timedelta(seconds=10 + override_max * interval_s + 1)
        cp_clock.set(cp_due)
        cp_trips = [
            e
            for e in cp_evaluator.tick()
            if e.kind == "trip" and e.tripwire == cad_id
        ]
        assert len(cp_trips) == 1
        assert "ev-cad-pre-cp-0001" in cp_trips[0].evidence

    hollow_service = ""
    hollow_tw: Any = None
    for svc in _reference_services():
        candidate = _contract(svc)
        for tripwire in candidate.tripwires:
            if tripwire.type == "schema" and tripwire.spec.get("where"):
                hollow_service = svc
                hollow_tw = tripwire
    assert hollow_tw is not None
    hollow_contract = _contract(hollow_service)
    hollow_spec = _schema_spec(hollow_tw)
    hollow_env = _envs_for_service(hollow_service)[0]
    hollow_dep = f"{hollow_service}/{hollow_env}"
    hollow_id = str(hollow_tw.id)
    assert hollow_spec.event is not None
    job_base = _base_entry(str(hollow_spec.event))
    hollow_clock = SimClock(T0)
    hollow_evaluator = Evaluator(
        [hollow_contract],
        [
            Deployment(
                id=hollow_dep,
                service=hollow_service,
                environment=hollow_env,
                parameters=_fleet_params(hollow_service, hollow_env),
            )
        ],
        hollow_clock,
    )
    violation_ids: list[str] = []
    schema_trips: list[TripEvent] = []
    for i in range(hollow_spec.consecutive):
        insert_id = f"ev-schema-viol-{i:04d}"
        violation_ids.append(insert_id)
        entry = _clone_entry(
            job_base,
            timestamp=T0 + timedelta(seconds=10 * i),
            insert_id=insert_id,
            env=hollow_env,
            service=hollow_service,
            run_id=f"ev-schema-run-{i:04d}",
        )
        entry.jsonPayload["status"] = "SUCCESS"
        entry.jsonPayload["indicatorsSynced"] = 0
        hollow_clock.set(entry.timestamp)
        schema_trips.extend(hollow_evaluator.ingest(entry))
        schema_trips.extend(hollow_evaluator.tick())
    trips_only = [
        e for e in schema_trips if e.kind == "trip" and e.tripwire == hollow_id
    ]
    assert len(trips_only) == 1
    for insert_id in violation_ids:
        assert insert_id in trips_only[0].evidence

    err_service = ""
    err_tw: Any = None
    for svc in _reference_services():
        candidate = _contract(svc)
        for tripwire in candidate.tripwires:
            if tripwire.type == "errorSignature":
                err_service = svc
                err_tw = tripwire
                break
        if err_tw is not None:
            break
    assert err_tw is not None
    err_contract = _contract(err_service)
    err_env = _envs_for_service(err_service)[0]
    err_spec = _error_spec(err_tw, _fleet_params(err_service, err_env))
    err_dep = f"{err_service}/{err_env}"
    err_id = str(err_tw.id)
    err_comp = str(err_tw.component)
    err_run_event = "connector.run.completed"
    err_warn_event = "connector.run.warning"
    if "job" in err_comp:
        err_run_event = "job.run.completed"
        err_warn_event = "job.run.error"
    err_done_base = _base_entry(err_run_event)
    err_warn_base = _base_entry(err_warn_event)
    err_clock = SimClock(T0)
    err_evaluator = Evaluator(
        [err_contract],
        [
            Deployment(
                id=err_dep,
                service=err_service,
                environment=err_env,
                parameters=_fleet_params(err_service, err_env),
            )
        ],
        err_clock,
    )
    bad_ids: list[str] = []
    err_out: list[TripEvent] = []
    for i in range(err_spec.threshold):
        run = f"ev-err-run-{i:04d}"
        at = T0 + timedelta(seconds=10 * i)
        warn_id = f"ev-err-warn-{i:04d}"
        bad_ids.append(warn_id)
        warn = _clone_entry(
            err_warn_base,
            timestamp=at,
            insert_id=warn_id,
            env=err_env,
            service=err_service,
            run_id=run,
            severity=str(err_spec.minSeverity),
        )
        err_clock.set(warn.timestamp)
        err_out.extend(err_evaluator.ingest(warn))
        done = _clone_entry(
            err_done_base,
            timestamp=at + timedelta(seconds=1),
            insert_id=f"ev-err-done-{i:04d}",
            env=err_env,
            service=err_service,
            run_id=run,
            severity="INFO",
        )
        err_clock.set(done.timestamp)
        err_out.extend(err_evaluator.ingest(done))
        err_out.extend(err_evaluator.tick())
    err_trips = [e for e in err_out if e.kind == "trip" and e.tripwire == err_id]
    assert len(err_trips) == 1
    for insert_id in bad_ids:
        assert insert_id in err_trips[0].evidence

    synth_service = ""
    synth_contract: Contract | None = None
    for svc in _reference_services():
        candidate = _contract(svc)
        try:
            _synthetic_params(candidate)
        except AssertionError:
            continue
        synth_service = svc
        synth_contract = candidate
        break
    assert synth_contract is not None
    _every, deadline_s, synth_id, _comp = _synthetic_params(synth_contract)
    synth_env = _envs_for_service(synth_service)[0]
    synth_dep = f"{synth_service}/{synth_env}"
    synth_clock = SimClock(T0)
    synth_evaluator = Evaluator(
        [synth_contract],
        [
            Deployment(
                id=synth_dep,
                service=synth_service,
                environment=synth_env,
                parameters=_fleet_params(synth_service, synth_env),
            )
        ],
        synth_clock,
    )
    provider = "canary-ev-0001"
    synth_evaluator.canary_planted(synth_dep, provider, T0)
    synth_clock.set(T0 + timedelta(seconds=deadline_s + 1))
    synth_trips = [
        e for e in synth_evaluator.tick() if e.kind == "trip" and e.tripwire == synth_id
    ]
    assert len(synth_trips) == 1
    assert provider in synth_trips[0].reason


def test_d4_t18_speed(
    _ref_contracts: dict[str, Contract],
    _ref_scenarios: dict[str, list[Scenario]],
):
    """D4-T18: both full libraries replay in under 30 seconds."""
    start = time.perf_counter()
    for service, contract in sorted(_ref_contracts.items()):
        board = scorecard(contract, _ref_scenarios[service])
        assert isinstance(board, Scorecard)
    elapsed = time.perf_counter() - start
    assert elapsed < 30
