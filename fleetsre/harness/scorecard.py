"""D4 scorecard harness: score one contract against its scenarios."""

from dataclasses import dataclass, field

from fleetsre.contracts import Contract
from fleetsre.harness.replay import ScenarioResult, run_scenario
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
    """Score one contract against its service's scenarios."""
    scored = [s for s in scenarios if s.service == contract.service]
    results = [run_scenario(contract, scenario) for scenario in scored]
    total = 0
    hit = 0
    for scenario, result in zip(scored, results):
        items = len(scenario.expected.trips) + len(scenario.expected.recoveries)
        total += items
        hit += items - len(result.missing)
    rate = hit / total if total else 1.0
    return Scorecard(
        results=results,
        passed=all(result.passed for result in results),
        expected_hit_rate=rate,
        false_positives=sum(len(result.unexpected) for result in results),
    )
