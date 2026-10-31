"""D4 tripwires: pure functions (no I/O, no clock reads)."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from fleetsre.contracts import ErrorSigSpec, SchemaSpec
from fleetsre.logmodel import LogEntry

# Cloud Logging severity order (worst entry wins).
_SEVERITY_ORDER = {
    "DEBUG": 0,
    "INFO": 1,
    "NOTICE": 2,
    "WARNING": 3,
    "ERROR": 4,
    "CRITICAL": 5,
    "ALERT": 6,
    "EMERGENCY": 7,
}


def cadence_due(
    last_event_at: datetime | None,
    armed_at: datetime,
    now: datetime,
    interval_s: int,
    max_silent: int,
) -> bool:
    """True when silence exceeds max_silent intervals.

    With no heartbeat yet, the window counts from armed_at.
    """
    ref = last_event_at if last_event_at is not None else armed_at
    return (now - ref).total_seconds() > max_silent * interval_s


def _lookup(entry: LogEntry, dotted: str) -> tuple[bool, Any]:
    """Look up a dotted field path rooted at the entry.

    Returns (found, value). ``jsonPayload`` addresses the payload dict;
    ``labels`` and ``resource.labels`` address the label maps.
    """
    parts = dotted.split(".")
    node: Any = {
        "jsonPayload": entry.jsonPayload,
        "labels": entry.labels,
        "resource": {"labels": entry.resource.labels},
    }
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False, None
    return True, node


def schema_violation(entry: LogEntry, spec: SchemaSpec) -> str | None:
    """Reason when entry violates the schema spec, else None.

    Entries whose event or ``where`` filters do not match are not
    applicable and return None (the engine ignores them entirely).
    """
    if spec.event is not None:
        if entry.jsonPayload.get("event") != spec.event:
            return None
    if spec.where:
        for key, want in spec.where.items():
            found, value = _lookup(entry, key)
            if not found or value != want:
                return None
    found, value = _lookup(entry, spec.field)
    if not found or value is None:
        if spec.required:
            return f"missing required field {spec.field}"
        return None
    if spec.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"field {spec.field} is not an integer: {value!r}"
    elif spec.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"field {spec.field} is not a number: {value!r}"
    elif spec.type == "string":
        if not isinstance(value, str):
            return f"field {spec.field} is not a string: {value!r}"
    elif spec.type == "boolean":
        if not isinstance(value, bool):
            return f"field {spec.field} is not a boolean: {value!r}"
    else:
        return f"unknown schema type {spec.type!r} for field {spec.field}"
    if spec.enum is not None and value not in spec.enum:
        return f"field {spec.field} value {value!r} not in {list(spec.enum)!r}"
    if spec.minimum is not None and isinstance(value, (int, float)):
        if value < spec.minimum:
            return (
                f"field {spec.field} value {value!r} "
                f"below minimum {spec.minimum!r}"
            )
    return None


def error_signature(
    recent_run_severities: Sequence[str], spec: ErrorSigSpec
) -> bool:
    """True when WARNING-or-worse runs in the window reach threshold.

    Only the last ``windowRuns`` entries count; a run counts when its
    severity is at or above ``minSeverity``.
    """
    order = _SEVERITY_ORDER.get(spec.minSeverity, 0)
    window = list(recent_run_severities)[-spec.windowRuns :]
    bad = sum(1 for sev in window if _SEVERITY_ORDER.get(sev, -1) >= order)
    return bad >= spec.threshold


def canary_missed(
    planted_at: datetime,
    seen_at: datetime | None,
    now: datetime,
    deadline_s: int,
) -> bool:
    """True when the planted canary missed its deadline.

    A canary seen at or before the deadline never misses, even late now;
    a canary seen after the deadline still missed.
    """
    if seen_at is not None:
        if (seen_at - planted_at).total_seconds() <= deadline_s:
            return False
        return (now - planted_at).total_seconds() > deadline_s
    return (now - planted_at).total_seconds() > deadline_s
