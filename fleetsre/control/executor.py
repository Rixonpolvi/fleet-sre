"""Journaled executor (d6a-pins 5, 18, 19). Stub: behavior raises."""

from typing import Any, Literal

from pydantic import BaseModel


class NotApproved(Exception):
    pass


class Conflict(Exception):
    def __init__(self, resource: str, expected: str, actual: str) -> None:
        super().__init__(
            f"conflict on {resource}: expected {expected}, got {actual}"
        )
        self.resource = resource
        self.expected = expected
        self.actual = actual


class InvalidPatch(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"invalid patch: {'; '.join(problems)}")
        self.problems = problems


class ApplyError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"apply failed: {detail}")
        self.detail = detail


class ApplyResult(BaseModel):
    proposal_hash: str
    outcome: Literal["applied", "already_applied"]
    api_calls: int
    resource_after: dict[str, Any]


class Executor:
    def __init__(
        self,
        chronicle: Any,
        approvals: Any,
        journal: Any,
        *,
        clock: Any,
    ) -> None:
        raise NotImplementedError

    def apply(self, incident: Any, proposal: Any) -> ApplyResult:
        raise NotImplementedError

    def recover(self) -> list[ApplyResult]:
        raise NotImplementedError
