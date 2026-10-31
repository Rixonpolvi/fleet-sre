"""Gates: pure checks run inside the working tools (spec.md §6.3, §6.5)."""

from typing import Any


def check_hypotheses(items: list[dict[str, Any]]) -> list[str]:
    raise NotImplementedError


def check_evidence(
    args: dict[str, Any],
    session: Any,
    log_query: Any,
    incident: Any,
    registry: Any,
) -> list[str]:
    raise NotImplementedError


def check_diagnosis(
    d: dict[str, Any],
    session: Any,
    incident: Any,
    contract: Any,
    log_query: Any,
    registry: Any,
) -> list[str]:
    raise NotImplementedError
