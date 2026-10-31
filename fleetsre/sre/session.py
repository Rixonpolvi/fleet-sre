"""Investigation session and session stores (saved after every turn)."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel

from fleetsre.sre.schemas import Evidence, Hypothesis


class CallRecord(BaseModel):
    tool: str = ""
    args: dict[str, Any] = {}
    result: dict[str, Any] = {}
    verdict: str = ""
    problems: list[str] = []


class Session(BaseModel):
    incident_id: str
    turn: int = 0
    read_calls: int = 0
    tokens: dict[str, int] = {}
    hypotheses: dict[str, Hypothesis] = {}
    evidence: dict[str, Evidence] = {}
    calls: dict[str, CallRecord] = {}
    refusals: dict[str, int] = {}
    status: Literal["running", "submitted", "needs_input"] = "running"
    reason: str = ""
    resumed_at_turns: list[int] = []
    history: list[dict[str, Any]] = []
    segment_start_turn: int = 0
    turn_limit: int = 24
    segment_reads: int = 0
    segment_tokens: int = 0
    refused_submissions: int = 0
    transcript_id: str = ""


class SessionStore(Protocol):
    def save(self, session: Session) -> None: ...

    def load(self, incident_id: str) -> Session: ...

    def append_transcript(
        self, transcript_id: str, record: dict[str, Any]
    ) -> None: ...

    def transcript(self, transcript_id: str) -> list[dict[str, Any]]: ...


class MemorySessionStore(SessionStore):
    """D5 stub: in-memory SessionStore."""

    def __init__(self) -> None:
        raise NotImplementedError

    def save(self, session: Session) -> None:
        raise NotImplementedError

    def load(self, incident_id: str) -> Session:
        raise NotImplementedError

    def append_transcript(
        self, transcript_id: str, record: dict[str, Any]
    ) -> None:
        raise NotImplementedError

    def transcript(self, transcript_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError
