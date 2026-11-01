"""SQLite session store (local adapter for SessionStore)."""

import json
import sqlite3
from pathlib import Path
from typing import Any


class SqliteSessionStore:
    """SessionStore backed by SQLite (spec.md §4.2)."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        conn = sqlite3.connect(self._path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "incident_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS transcripts ("
                "transcript_id TEXT NOT NULL, seq INTEGER NOT NULL, "
                "record TEXT NOT NULL, "
                "PRIMARY KEY (transcript_id, seq))"
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def save(self, session: Any) -> None:
        from fleetsre.sre.session import Session

        if not isinstance(session, Session):
            session = Session.model_validate(session)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (incident_id, data) "
                "VALUES (?, ?)",
                (session.incident_id, session.model_dump_json()),
            )
            conn.commit()
        finally:
            conn.close()

    def load(self, incident_id: str) -> Any:
        from fleetsre.sre.session import Session

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT data FROM sessions WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise KeyError(f"unknown incident: {incident_id}")
        return Session.model_validate_json(row[0])

    def append_transcript(
        self, transcript_id: str, record: dict[str, Any]
    ) -> None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM transcripts "
                "WHERE transcript_id = ?",
                (transcript_id,),
            ).fetchone()
            seq = int(row[0]) + 1
            conn.execute(
                "INSERT INTO transcripts (transcript_id, seq, record) "
                "VALUES (?, ?, ?)",
                (transcript_id, seq, json.dumps(record)),
            )
            conn.commit()
        finally:
            conn.close()

    def transcript(self, transcript_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT record FROM transcripts WHERE transcript_id = ? "
                "ORDER BY seq",
                (transcript_id,),
            ).fetchall()
        finally:
            conn.close()
        return [json.loads(row[0]) for row in rows]
