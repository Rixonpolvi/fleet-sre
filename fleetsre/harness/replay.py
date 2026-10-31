"""D4 replay harness stub."""

from dataclasses import dataclass, field

from fleetsre.contracts import Contract
from fleetsre.evaluator.engine import TripEvent
from fleetsre.harness.scenario import Expected, Scenario


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


def run_scenario(
    contract: Contract, scenario: Scenario, *, tick_seconds: int = 15
) -> ScenarioResult:
    """Stub: replay one scenario against its contract."""
    raise NotImplementedError
