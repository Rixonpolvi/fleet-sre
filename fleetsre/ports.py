"""Ports: LogSink, LogQuery, ChronicleClient, StateStore and in-memory fakes."""

from datetime import datetime
from typing import Any, Protocol

from fleetsre.logmodel import LogEntry


class LogSink(Protocol):
    def write(self, entry: LogEntry) -> None: ...


class MemorySink(LogSink):
    entries: list[LogEntry]

    def __init__(self) -> None:
        self.entries = []

    def write(self, entry: LogEntry) -> None:
        self.entries.append(entry)


class LogQuery(Protocol):
    """Read log entries by time range or by insertId (spec.md §4.2)."""

    def entries(self, start: datetime, end: datetime) -> list[LogEntry]: ...

    def get(self, insert_id: str) -> LogEntry | None: ...


class MemoryLogQuery(LogQuery):
    """In-memory LogQuery over a MemorySink (local adapter, spec.md §4.2)."""

    def __init__(self, sink: MemorySink) -> None:
        self._sink = sink

    def entries(self, start: datetime, end: datetime) -> list[LogEntry]:
        return [
            e for e in self._sink.entries
            if start <= e.timestamp <= end
        ]

    def get(self, insert_id: str) -> LogEntry | None:
        for entry in self._sink.entries:
            if entry.insertId == insert_id:
                return entry
        return None


class ChronicleError(Exception):
    """Chronicle API failure carrying the Google error body."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Chronicle error {status}: {body}")


class ChronicleClient(Protocol):
    """Read/write Chronicle surface used by tools (read) and executor (write)."""

    @property
    def identity(self) -> str: ...

    def get(self, name: str) -> dict[str, Any]: ...

    def list_connector_logs(
        self, name: str, page_size: int
    ) -> dict[str, Any]: ...

    def list_job_logs(self, name: str, page_size: int) -> dict[str, Any]: ...

    def execute_test(self, name: str) -> dict[str, Any]: ...

    def patch(
        self, name: str, body: dict[str, Any], update_mask: list[str]
    ) -> dict[str, Any]: ...

    def run_on_demand(
        self, name: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...

    def set_logs_collection(
        self, name: str, enabled: bool
    ) -> dict[str, Any]: ...


class StateStore(Protocol):
    """Trip history and approved-change records per deployment."""

    def record_trip(self, event: Any) -> None: ...

    def record_change(
        self, deployment: str, record: dict[str, Any]
    ) -> None: ...

    def trip_history(self, deployment: str) -> list[dict[str, Any]]: ...


class MemoryStateStore(StateStore):
    """In-memory StateStore: trip events and change records per deployment."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def record_trip(self, event: Any) -> None:
        at = getattr(event, "at", None)
        self._records.append({
            "kind": getattr(event, "kind", "trip"),
            "deployment": getattr(event, "deployment", ""),
            "tripwire": getattr(event, "tripwire", ""),
            "at": at.isoformat() if at is not None else "",
            "reason": getattr(event, "reason", ""),
            "evidence": list(getattr(event, "evidence", ())),
        })

    def record_change(
        self, deployment: str, record: dict[str, Any]
    ) -> None:
        stored = dict(record)
        stored["deployment"] = deployment
        stored.setdefault("kind", "change")
        self._records.append(stored)

    def trip_history(self, deployment: str) -> list[dict[str, Any]]:
        return [
            dict(r) for r in self._records
            if r.get("deployment") == deployment
        ]
