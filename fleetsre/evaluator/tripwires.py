"""D4 tripwires: pure functions (no I/O, no clock reads). Stub."""

from collections.abc import Sequence
from datetime import datetime

from fleetsre.contracts import ErrorSigSpec, SchemaSpec
from fleetsre.logmodel import LogEntry


def cadence_due(
    last_event_at: datetime | None,
    armed_at: datetime,
    now: datetime,
    interval_s: int,
    max_silent: int,
) -> bool:
    """Stub: True when silence exceeds max_silent intervals."""
    raise NotImplementedError


def schema_violation(entry: LogEntry, spec: SchemaSpec) -> str | None:
    """Stub: reason when entry violates the schema spec, else None."""
    raise NotImplementedError


def error_signature(
    recent_run_severities: Sequence[str], spec: ErrorSigSpec
) -> bool:
    """Stub: True when WARNING-or-worse runs in the window reach threshold."""
    raise NotImplementedError


def canary_missed(
    planted_at: datetime,
    seen_at: datetime | None,
    now: datetime,
    deadline_s: int,
) -> bool:
    """Stub: True when the planted canary missed its deadline."""
    raise NotImplementedError
