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
    turn: int = 0


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
    read_limit: int = 12
    token_limit: int = 400_000
    plan: list[Any] = []


class SessionStore(Protocol):
    def save(self, session: Session) -> None: ...

    def load(self, incident_id: str) -> Session: ...

    def append_transcript(
        self, transcript_id: str, record: dict[str, Any]
    ) -> None: ...

    def transcript(self, transcript_id: str) -> list[dict[str, Any]]: ...


class MemorySessionStore(SessionStore):
    """In-memory SessionStore."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._transcripts: dict[str, list[dict[str, Any]]] = {}

    def save(self, session: Session) -> None:
        self._sessions[session.incident_id] = session.model_copy(deep=True)

    def load(self, incident_id: str) -> Session:
        try:
            return self._sessions[incident_id].model_copy(deep=True)
        except KeyError:
            raise KeyError(
                f"unknown incident: {incident_id}"
            ) from None

    def append_transcript(
        self, transcript_id: str, record: dict[str, Any]
    ) -> None:
        self._transcripts.setdefault(transcript_id, []).append(
            dict(record)
        )

    def transcript(self, transcript_id: str) -> list[dict[str, Any]]:
        return [
            dict(r) for r in self._transcripts.get(transcript_id, [])
        ]
