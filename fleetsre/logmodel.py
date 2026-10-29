"""D1 log model stub: Cloud Logging LogEntry shape (mocks.md §5)."""

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class MonitoredResource(BaseModel):
    type: str
    labels: dict[str, str]


class LogEntry(BaseModel):
    insertId: str
    timestamp: datetime
    severity: str
    logName: str
    resource: MonitoredResource
    labels: dict[str, str]
    jsonPayload: dict[str, Any]


def read_jsonl(path: str | Path) -> list[LogEntry]:
    """Read a JSONL file as a list of LogEntry, one per non-blank line."""
    entries: list[LogEntry] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                entries.append(LogEntry.model_validate_json(line))
    return entries
