"""SQLite session store (local adapter for SessionStore)."""

from pathlib import Path
from typing import Any


class SqliteSessionStore:
    """D5 stub: SessionStore backed by SQLite."""

    def __init__(self, path: str | Path) -> None:
        raise NotImplementedError

    def save(self, session: Any) -> None:
        raise NotImplementedError

    def load(self, incident_id: str) -> Any:
        raise NotImplementedError

    def append_transcript(
        self, transcript_id: str, record: dict[str, Any]
    ) -> None:
        raise NotImplementedError

    def transcript(self, transcript_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError
