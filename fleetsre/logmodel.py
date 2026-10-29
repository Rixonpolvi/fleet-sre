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
    """Read a JSONL file as LogEntry list. Session B implements."""
    raise NotImplementedError
