"""Cloud Logging sink and query adapters (docs/d7-pins.md §Interfaces 2).

Stub for the D7 tests PR: every name raises NotImplementedError.
Session B implements them on google-cloud-logging.
"""

from datetime import datetime
from typing import Any

from fleetsre.logmodel import LogEntry


def to_cloud_entry(entry: LogEntry, project: str) -> dict[str, Any]:
    """Convert a LogEntry to the Cloud Logging JSON LogEntry (d7-pins 2)."""
    raise NotImplementedError


def from_cloud_entry(data: dict[str, Any]) -> LogEntry:
    """Convert a Cloud Logging JSON LogEntry back to a LogEntry (d7-pins 2)."""
    raise NotImplementedError


def build_filter(start: datetime, end: datetime, extra: str = "") -> str:
    """Build the entries.list filter text (d7-pins 2)."""
    raise NotImplementedError


def build_get_filter(insert_id: str) -> str:
    """Build the get-by-insertId filter text (d7-pins 2)."""
    raise NotImplementedError


class CloudLoggingSink:
    """LogSink over Cloud Logging entries.write (d7-pins 2)."""

    def __init__(self, project: str, *, credentials: Any = None) -> None:
        raise NotImplementedError

    def write(self, entry: LogEntry) -> None:
        raise NotImplementedError


class CloudLoggingQuery:
    """LogQuery over Cloud Logging entries.list (d7-pins 2)."""

    def __init__(
        self,
        project: str,
        *,
        credentials: Any = None,
        extra_filter: str = "",
    ) -> None:
        raise NotImplementedError

    def entries(self, start: datetime, end: datetime) -> list[LogEntry]:
        raise NotImplementedError

    def get(self, insert_id: str) -> LogEntry | None:
        raise NotImplementedError
