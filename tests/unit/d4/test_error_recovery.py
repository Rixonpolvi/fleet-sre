"""Unit tests for D4 errorSignature recovery semantics pinned by the goal.

Acceptance T7 feeds only clean runs after a trip, so it cannot tell
window-based recovery ("window drops below threshold") from streak-based
recovery ("window-threshold+1 consecutive clean runs"). The flap-severe
reference scenario (70% ERROR runs, one trip, no recoveries) requires the
streak: the trailing window dips below threshold dozens of times while no
three consecutive clean runs ever occur.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleetsre.clock import SimClock
from fleetsre.contracts import load_contract
from fleetsre.evaluator.engine import Evaluator, TripEvent
from fleetsre.logmodel import LogEntry, MonitoredResource
from fleetsre.mock.fleet import load_fleet
from fleetsre.registry import Deployment

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)

_DEP = "alert-ingestion/alpha"
_TW = "connector-errors"


def _evaluator(clock: SimClock) -> Evaluator:
    contract = load_contract(ROOT / "contracts" / "alert-ingestion.json")
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    params = dict(fleet.environments["alpha"].deployments["alert-ingestion"].parameters)
    return Evaluator(
        [contract],
        [Deployment(id=_DEP, service="alert-ingestion", environment="alpha", parameters=params)],
        clock,
    )


def _entry(ts: datetime, insert_id: str, event: str, run: str, severity: str) -> LogEntry:
    return LogEntry(
        insertId=insert_id,
        timestamp=ts,
        severity=severity,
        logName="projects/fleet-sre-demo/logs/chronicle-soar",
        resource=MonitoredResource(
            type="k8s_container",
            labels={
                "project_id": "fleet-sre-demo",
                "location": "us-central1",
                "cluster_name": "mock-soar",
                "namespace_name": "chronicle-soar",
                "pod_name": "python-0",
                "container_name": "python",
            },
        ),
        labels={
            "integration_name": "MockSentinel",
            "integration_version": "1.0",
            "connector_name": "Mock Sentinel Incidents Connector",
            "connector_instance": "alpha-sentinel",
        },
        jsonPayload={"event": event, "runId": run, "environment": "alpha"},
    )


def _feed(evaluator: Evaluator, clock: SimClock, entries: list[LogEntry]) -> list[TripEvent]:
    out: list[TripEvent] = []
    for entry in entries:
        clock.set(entry.timestamp)
        out.extend(evaluator.ingest(entry))
    out.extend(evaluator.tick())
    return out


def _run_entries(index: int, bad: bool) -> list[LogEntry]:
    base = T0 + timedelta(seconds=100 * index)
    run = f"unit-err-run-{index:04d}"
    entries = []
    if bad:
        entries.append(_entry(base, f"unit-err-w-{index:04d}", "connector.run.warning", run, "WARNING"))
    entries.append(
        _entry(base + timedelta(seconds=1), f"unit-err-d-{index:04d}", "connector.run.completed", run, "INFO")
    )
    return entries


def test_no_recovery_while_window_hovers_at_threshold() -> None:
    """Window dips below threshold without 3 consecutive clean runs: no recovery."""
    clock = SimClock(T0)
    evaluator = _evaluator(clock)
    for i in range(8):
        events = _feed(evaluator, clock, _run_entries(i, bad=True))
    trips = [e for e in events if e.kind == "trip" and e.tripwire == _TW]
    assert len(trips) == 1
    assert evaluator.status()[_DEP][_TW] == "tripped"
    # Hover: pairs of clean runs separated by bad ones. The trailing-10
    # window drops to 6-7 bad, but no 3 consecutive clean runs occur.
    pattern = [False, False, True] * 4
    for offset, bad in enumerate(pattern):
        events = _feed(evaluator, clock, _run_entries(100 + offset, bad=bad))
        assert [e for e in events if e.tripwire == _TW] == []
    assert evaluator.status()[_DEP][_TW] == "tripped"
    # Three consecutive clean runs recover exactly once.
    for offset in range(3):
        events = _feed(evaluator, clock, _run_entries(200 + offset, bad=False))
        recs = [e for e in events if e.kind == "recovery" and e.tripwire == _TW]
        if offset < 2:
            assert recs == []
        else:
            assert len(recs) == 1
    assert evaluator.status()[_DEP][_TW] == "ok"


def test_runs_judged_at_completion() -> None:
    """Warnings alone never trip; the trip fires when the runs complete."""
    clock = SimClock(T0)
    evaluator = _evaluator(clock)
    warns = []
    for i in range(8):
        warns.extend(_run_entries(i, bad=True)[:1])
    events = _feed(evaluator, clock, warns)
    assert [e for e in events if e.tripwire == _TW] == []
    assert evaluator.status()[_DEP][_TW] == "ok"
    dones = []
    for i in range(8):
        done = _run_entries(i, bad=True)[1]
        dones.append(done)
    events = _feed(evaluator, clock, dones)
    trips = [e for e in events if e.kind == "trip" and e.tripwire == _TW]
    assert len(trips) == 1
    assert evaluator.status()[_DEP][_TW] == "tripped"
