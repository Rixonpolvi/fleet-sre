"""Unit tests: cadence recovers only on a heartbeat that clears due-ness.

A late delivery with a stale timestamp must not flip a tripped cadence
tripwire to ok: the next tick would re-trip it (trip -> recovery -> trip).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleetsre.clock import SimClock
from fleetsre.contracts import load_contract
from fleetsre.evaluator.engine import Evaluator
from fleetsre.logmodel import LogEntry, MonitoredResource
from fleetsre.mock.fleet import load_fleet
from fleetsre.registry import Deployment

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)

_DEP = "alert-ingestion/alpha"
_TW = "dead-connector"
_EVENT = "connector.run.completed"


def _evaluator(clock: SimClock) -> Evaluator:
    contract = load_contract(ROOT / "contracts" / "alert-ingestion.json")
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    params = dict(
        fleet.environments["alpha"].deployments["alert-ingestion"].parameters
    )
    return Evaluator(
        [contract],
        [
            Deployment(
                id=_DEP,
                service="alert-ingestion",
                environment="alpha",
                parameters=params,
            )
        ],
        clock,
    )


def _entry(ts: datetime, insert_id: str) -> LogEntry:
    return LogEntry(
        insertId=insert_id,
        timestamp=ts,
        severity="INFO",
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
        jsonPayload={
            "event": _EVENT,
            "runId": insert_id,
            "environment": "alpha",
        },
    )


def _trip(clock: SimClock, evaluator: Evaluator) -> datetime:
    due = T0 + timedelta(seconds=200)
    clock.set(due)
    trips = [
        e for e in evaluator.tick() if e.kind == "trip" and e.tripwire == _TW
    ]
    assert len(trips) == 1
    assert evaluator.status()[_DEP][_TW] == "tripped"
    return due


def test_stale_heartbeat_before_silence_window_no_recovery() -> None:
    """Stale heartbeat (still due) recovers nothing; next tick is silent."""
    clock = SimClock(T0)
    evaluator = _evaluator(clock)
    due = _trip(clock, evaluator)
    stale = _entry(T0 + timedelta(seconds=5), "unit-cad-stale-0001")
    out = evaluator.ingest(stale)
    assert [e for e in out if e.tripwire == _TW] == []
    assert evaluator.status()[_DEP][_TW] == "tripped"
    assert clock.now() == due
    assert evaluator.tick() == []


def test_pre_change_point_heartbeat_no_recovery() -> None:
    """Heartbeat from before an active change point recovers nothing."""
    clock = SimClock(T0)
    evaluator = _evaluator(clock)
    entry_at = T0 + timedelta(seconds=10)
    clock.set(entry_at)
    evaluator.enter_change_point(
        _DEP,
        "connector",
        duration_s=1800,
        overrides={_TW: {"maxSilentIntervals": 2}},
    )
    due = entry_at + timedelta(seconds=2 * 60 + 1)
    clock.set(due)
    trips = [
        e for e in evaluator.tick() if e.kind == "trip" and e.tripwire == _TW
    ]
    assert len(trips) == 1
    assert evaluator.status()[_DEP][_TW] == "tripped"
    stale = _entry(T0 + timedelta(seconds=5), "unit-cad-cp-stale-0001")
    out = evaluator.ingest(stale)
    assert [e for e in out if e.tripwire == _TW] == []
    assert evaluator.status()[_DEP][_TW] == "tripped"


def test_fresh_heartbeat_recovers_once() -> None:
    """Fresh heartbeat after a trip recovers exactly once."""
    clock = SimClock(T0)
    evaluator = _evaluator(clock)
    due = _trip(clock, evaluator)
    beat_at = due + timedelta(seconds=5)
    clock.set(beat_at)
    beat = _entry(beat_at, "unit-cad-fresh-0001")
    out = evaluator.ingest(beat)
    recs = [e for e in out if e.kind == "recovery" and e.tripwire == _TW]
    assert len(recs) == 1
    assert evaluator.status()[_DEP][_TW] == "ok"
