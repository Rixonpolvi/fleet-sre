"""Change-point verifier (d6a-pins 6, 15). Stub: behavior raises."""

from typing import Any, Literal


class Verifier:
    def __init__(
        self, evaluator: Any, registry: Any, clock: Any
    ) -> None:
        raise NotImplementedError

    def start(
        self,
        incident: Any,
        diagnosis: dict[str, Any],
        *,
        changed_resource: str | None,
    ) -> None:
        raise NotImplementedError

    def check(self, incident_id: str) -> Literal["pending", "verified", "failed"]:
        raise NotImplementedError

    def active(self) -> list[str]:
        raise NotImplementedError
