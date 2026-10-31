"""D4 scorecard harness stub."""

from dataclasses import dataclass, field

from fleetsre.contracts import Contract
from fleetsre.harness.replay import ScenarioResult
from fleetsre.harness.scenario import Scenario


@dataclass
class Scorecard:
    results: list[ScenarioResult] = field(default_factory=list)
    passed: bool = False
    expected_hit_rate: float = 0.0
    false_positives: int = 0


def scorecard(
    contract: Contract, scenarios: list[Scenario]
) -> Scorecard:
    """Stub: score one contract against its service's scenarios."""
    raise NotImplementedError
