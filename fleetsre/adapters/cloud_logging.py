"""Cloud Logging sink and query adapters (docs/d7-pins.md §Interfaces 2).

The local log format is already Cloud Logging's ``LogEntry`` JSON
(spec.md §4.2), so the converters below only re-point the entry at the
GCP project. Neither class retries: a ``PermissionDenied`` propagates
to the caller (d7-pins 2, 6).
"""

import copy
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import google.auth.credentials
from google.api.monitored_resource_pb2 import MonitoredResource
from google.cloud.logging_v2.services.logging_service_v2 import (
    LoggingServiceV2Client,
)
from google.cloud.logging_v2.types import (
    ListLogEntriesRequest,
    LogEntry as ProtoLogEntry,
    WriteLogEntriesRequest,
)
from google.protobuf.timestamp_pb2 import Timestamp

from fleetsre.logmodel import LogEntry, MonitoredResource as LogResource
from fleetsre.ports import LogQuery, LogSink

LOG_ID = "chronicle-soar"
RESOURCE_TYPE = "k8s_container"
NAMESPACE = "chronicle-soar"
BASE_FILTER = (
    'resource.type="k8s_container"'
    ' AND resource.labels.namespace_name="chronicle-soar"'
)
GET_WINDOW = timedelta(hours=24)

# Cloud LogSeverity numbers
# (https://cloud.google.com/logging/docs/reference/v2/rest/v2/LogEntry#logseverity).
_SEVERITY_NUMBERS = {
    "DEFAULT": 0,
    "DEBUG": 100,
    "INFO": 200,
    "NOTICE": 300,
    "WARNING": 400,
    "ERROR": 500,
    "CRITICAL": 600,
    "ALERT": 700,
    "EMERGENCY": 800,
}
_SEVERITY_NAMES = {number: name for name, number in _SEVERITY_NUMBERS.items()}


def _rfc3339(value: datetime) -> str:
    """Format a datetime as an RFC 3339 UTC `Z` string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    """Parse an RFC 3339 string (or pass a datetime through)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_cloud_entry(entry: LogEntry, project: str) -> dict[str, Any]:
    """Convert a LogEntry to the Cloud Logging JSON LogEntry (d7-pins 2)."""
    resource_labels = dict(entry.resource.labels)
    resource_labels["project_id"] = project
    return {
        "logName": f"projects/{project}/logs/{LOG_ID}",
        "resource": {"type": entry.resource.type, "labels": resource_labels},
        "insertId": entry.insertId,
        "timestamp": _rfc3339(entry.timestamp),
        "severity": entry.severity,
        "labels": dict(entry.labels),
        "jsonPayload": copy.deepcopy(entry.jsonPayload),
    }


def from_cloud_entry(data: dict[str, Any]) -> LogEntry:
    """Convert a Cloud Logging JSON LogEntry back to a LogEntry (d7-pins 2).

    Pure: it never mutates its input. Numbers in ``jsonPayload`` stay as
    the cloud returns them (ints read back as floats; d7-pins 8), and the
    README's limits should note that once D8 writes it.
    """
    resource = data.get("resource") or {}
    return LogEntry(
        insertId=data["insertId"],
        timestamp=_parse_timestamp(data["timestamp"]),
        severity=data.get("severity", "DEFAULT"),
        logName=data["logName"],
        resource=LogResource(
            type=resource.get("type", RESOURCE_TYPE),
            labels=dict(resource.get("labels") or {}),
        ),
        labels=dict(data.get("labels") or {}),
        jsonPayload=copy.deepcopy(data.get("jsonPayload") or {}),
    )


def build_filter(start: datetime, end: datetime, extra: str = "") -> str:
    """Build the entries.list filter text (d7-pins 2)."""
    filt = (
        f"{BASE_FILTER}"
        f' AND timestamp>="{_rfc3339(start)}"'
        f' AND timestamp<"{_rfc3339(end)}"'
    )
    if extra:
        filt += f" AND {extra}"
    return filt


def build_get_filter(insert_id: str) -> str:
    """Build the get-by-insertId filter text (d7-pins 2)."""
    return f'{BASE_FILTER} AND insertId="{insert_id}"'


def _to_proto_entry(entry: LogEntry, project: str) -> ProtoLogEntry:
    """Render a LogEntry as a logging_v2 LogEntry message."""
    timestamp = _parse_timestamp(entry.timestamp)
    stamped = Timestamp()
    stamped.FromDatetime(timestamp)
    resource_labels = dict(entry.resource.labels)
    resource_labels["project_id"] = project
    return ProtoLogEntry(
        log_name=f"projects/{project}/logs/{LOG_ID}",
        resource=MonitoredResource(
            type=entry.resource.type, labels=resource_labels
        ),
        timestamp=stamped,
        severity=_SEVERITY_NUMBERS.get(entry.severity, 0),
        insert_id=entry.insertId,
        labels=dict(entry.labels),
        json_payload=copy.deepcopy(entry.jsonPayload),
    )


def _plain(value: Any) -> Any:
    """Recursively convert Struct maps/lists to plain dicts/lists."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        return value
    return [_plain(item) for item in value]


def _proto_to_dict(entry: ProtoLogEntry) -> dict[str, Any]:
    """Render a returned message as the JSON shape from_cloud_entry reads."""
    return {
        "logName": entry.log_name,
        "resource": {
            "type": entry.resource.type,
            "labels": dict(entry.resource.labels),
        },
        "insertId": entry.insert_id,
        "timestamp": entry.timestamp,
        "severity": _SEVERITY_NAMES.get(int(entry.severity), "DEFAULT"),
        "labels": dict(entry.labels),
        "jsonPayload": _plain(entry.json_payload),
    }


class CloudLoggingSink(LogSink):
    """LogSink over Cloud Logging entries.write (d7-pins 2)."""

    def __init__(
        self,
        project: str,
        *,
        credentials: google.auth.credentials.Credentials | None = None,
    ) -> None:
        self._project = project
        self._client = LoggingServiceV2Client(credentials=credentials)

    def write(self, entry: LogEntry) -> None:
        request = WriteLogEntriesRequest(
            entries=[_to_proto_entry(entry, self._project)],
            partial_success=False,
        )
        self._client.write_log_entries(request=request)


class CloudLoggingQuery(LogQuery):
    """LogQuery over Cloud Logging entries.list (d7-pins 2)."""

    def __init__(
        self,
        project: str,
        *,
        credentials: google.auth.credentials.Credentials | None = None,
        extra_filter: str = "",
    ) -> None:
        self._project = project
        self._extra_filter = extra_filter
        self._client = LoggingServiceV2Client(credentials=credentials)

    def _list(self, filt: str) -> Any:
        request = ListLogEntriesRequest(
            resource_names=[f"projects/{self._project}"],
            filter=filt,
            order_by="timestamp asc",
        )
        return self._client.list_log_entries(request=request)

    def entries(self, start: datetime, end: datetime) -> list[LogEntry]:
        """List entries in [start, end), oldest first (d7-pins 2)."""
        filt = build_filter(start, end, self._extra_filter)
        return [from_cloud_entry(_proto_to_dict(e)) for e in self._list(filt)]

    def get(self, insert_id: str) -> LogEntry | None:
        """Return one entry by insertId, or None (d7-pins 2)."""
        end = datetime.now(timezone.utc)
        start = end - GET_WINDOW
        filt = (
            f"{build_get_filter(insert_id)}"
            f' AND timestamp>="{_rfc3339(start)}"'
            f' AND timestamp<"{_rfc3339(end)}"'
        )
        for found in self._list(filt):
            return from_cloud_entry(_proto_to_dict(found))
        return None
