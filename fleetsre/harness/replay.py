"""D4 replay harness: run one chaos scenario against a contract."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleetsre.clock import SimClock
from fleetsre.contracts import Contract
from fleetsre.evaluator.canary import CanaryScheduler
from fleetsre.evaluator.engine import Evaluator, TripEvent
from fleetsre.harness.scenario import Expected, Scenario
from fleetsre.mock.fleet import load_fleet
from fleetsre.mock.world import World
from fleetsre.ports import MemorySink
from fleetsre.registry import deployments_from_fleet

T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)


@dataclass
class ScenarioResult:
    """Result of replaying one scenario.

    latencies maps (kind, deployment_id, tripwire) to seconds, where kind is
    "trip" or "recovery" and deployment_id is "<service>/<environment>".
    missing holds (kind, deployment_id, tripwire) for expected items with no
    actual match within withinSeconds. unexpected holds actual events matched
    by no expected item (mustNotTrip violations included).
    """

    scenario_id: str
    expected: Expected
    actual: list[TripEvent] = field(default_factory=list)
    latencies: dict[tuple[str, str, str], float] = field(default_factory=dict)
    missing: list[tuple[str, str, str]] = field(default_factory=list)
    unexpected: list[TripEvent] = field(default_factory=list)
    passed: bool = False


def _fleet_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "fixtures"
        / "fleet.yaml"
    )


def _latest_fault_at(
    scenario: Scenario, environment: str, at: datetime
) -> datetime | None:
    best: datetime | None = None
    for fault in scenario.faults:
        ref = T0 + timedelta(seconds=fault.at)
        if fault.environment == environment and ref <= at:
            if best is None or ref > best:
                best = ref
    return best


def _latest_clear_at(
    scenario: Scenario, environment: str, at: datetime
) -> datetime | None:
    best: datetime | None = None
    for fault in scenario.faults:
        if fault.environment != environment or fault.clearAt is None:
            continue
        ref = T0 + timedelta(seconds=fault.clearAt)
        if ref <= at and (best is None or ref > best):
            best = ref
    if best is not None:
        return best
    return _latest_fault_at(scenario, environment, at)


def run_scenario(
    contract: Contract, scenario: Scenario, *, tick_seconds: int = 15
) -> ScenarioResult:
    """Replay one scenario against its contract on a SimClock."""
    fleet = load_fleet(_fleet_path())
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(fleet, scenario.seed, clock, sink)
    items: list[dict[str, object]] = []
    for fault in scenario.faults:
        item: dict[str, object] = {
            "at": fault.at,
            "type": fault.type,
            "environment": fault.environment,
            "params": dict(fault.params),
        }
        target = getattr(fault, "target", None)
        if target is not None:
            item["target"] = target
        if fault.clearAt is not None:
            item["clearAt"] = fault.clearAt
        items.append(item)
    world.schedule(items)
    deployments = deployments_from_fleet(fleet, contract, scenario.deployments)
    evaluator = Evaluator([contract], deployments, clock)
    scheduler = CanaryScheduler(world, evaluator, [contract], clock)
    actual: list[TripEvent] = []
    consumed = 0
    steps = scenario.durationSeconds // tick_seconds
    for step in range(steps + 1):
        tick_at = T0 + timedelta(seconds=step * tick_seconds)
        clock.set(tick_at)
        scheduler.tick()
        world.step_until(tick_at)
        entries = sink.entries
        while consumed < len(entries):
            actual.extend(evaluator.ingest(entries[consumed]))
            consumed += 1
        actual.extend(evaluator.tick())

    latencies: dict[tuple[str, str, str], float] = {}
    missing: list[tuple[str, str, str]] = []
    unexpected: list[TripEvent] = []
    matched = [False] * len(actual)
    by_key: dict[tuple[str, str, str], list[int]] = {}
    for index, event in enumerate(actual):
        by_key.setdefault(
            (event.kind, event.deployment, event.tripwire), []
        ).append(index)
    for trip in scenario.expected.trips:
        dep_id = f"{scenario.service}/{trip.deployment}"
        key = ("trip", dep_id, trip.tripwire)
        hit = False
        for index in by_key.get(key, []):
            if matched[index]:
                continue
            ref = _latest_fault_at(
                scenario, trip.deployment, actual[index].at
            )
            if ref is None:
                continue
            latency = (actual[index].at - ref).total_seconds()
            if 0 <= latency <= trip.withinSeconds:
                matched[index] = True
                latencies[key] = latency
                hit = True
                break
        if not hit:
            missing.append(key)
    for recovery in scenario.expected.recoveries:
        dep_id = f"{scenario.service}/{recovery.deployment}"
        key = ("recovery", dep_id, recovery.tripwire)
        hit = False
        for index in by_key.get(key, []):
            if matched[index]:
                continue
            ref = _latest_clear_at(
                scenario, recovery.deployment, actual[index].at
            )
            if ref is None:
                continue
            latency = (actual[index].at - ref).total_seconds()
            if 0 <= latency <= recovery.withinSeconds:
                matched[index] = True
                latencies[key] = latency
                hit = True
                break
        if not hit:
            missing.append(key)
    for index, event in enumerate(actual):
        if not matched[index]:
            unexpected.append(event)
    return ScenarioResult(
        scenario_id=scenario.id,
        expected=scenario.expected,
        actual=actual,
        latencies=latencies,
        missing=missing,
        unexpected=unexpected,
        passed=(not missing and not unexpected),
    )
