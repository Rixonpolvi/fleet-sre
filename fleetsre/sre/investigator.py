"""Investigator: gated, budgeted loop ending in a diagnosis or NEEDS_INPUT."""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class InvestigationResult:
    diagnosis: Any | None
    status: Literal["submitted", "needs_input"]
    reason: str
    transcript_id: str
    turns: int
    read_calls: int
    tokens: dict[str, Any] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)


def investigate(
    incident: Any,
    tools: Any,
    gateway: Any,
    store: Any,
    *,
    max_turns: int = 24,
    max_read_calls: int = 12,
    max_tokens: int = 400_000,
) -> InvestigationResult:
    raise NotImplementedError


def resume(
    incident: Any,
    tools: Any,
    gateway: Any,
    store: Any,
    *,
    message: str,
    extra_turns: int = 8,
    extra_reads: int = 4,
) -> InvestigationResult:
    raise NotImplementedError
