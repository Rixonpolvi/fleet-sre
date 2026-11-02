"""Authoring loop: brief -> contract -> scorecard -> feedback (d6b-pins 5-13).

Stub: versions and results are real records; every behavior raises.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from fleetsre.contracts import Contract
from fleetsre.harness.scorecard import Scorecard, scorecard


@dataclass
class AuthoringVersion:
    index: int
    status: Literal["invalid_json", "invalid_contract", "failed", "passed"]
    problems: list[str] = field(default_factory=list)
    contract: dict[str, Any] | None = None
    scorecard: Scorecard | None = None
    feedback: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass
class AuthoringResult:
    passed: bool
    stop_reason: Literal["passed", "max_revisions", "model_unavailable"]
    contract: Contract | None
    versions: list[AuthoringVersion] = field(default_factory=list)
    scorecards: list[Scorecard] = field(default_factory=list)
    model: str = ""
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0


def author_contract(
    brief: Any,
    samples: list[Any],
    scenarios: list[Any],
    gateway: Any,
    *,
    max_revisions: int = 3,
    scorer: Callable[[Contract, list[Any]], Scorecard] = scorecard,
) -> AuthoringResult:
    """Run the authoring loop (d6b-pins 5-7, 9, 12-13)."""
    raise NotImplementedError


def render_brief(
    brief: Any, samples: list[Any], schema: dict[str, Any], *, versions: int
) -> str:
    """Build the first user message (d6b-pins 8)."""
    raise NotImplementedError


def render_feedback(version: AuthoringVersion, scenarios: list[Any]) -> str:
    """Build the feedback message after a version (d6b-pins 10)."""
    raise NotImplementedError
