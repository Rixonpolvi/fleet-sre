"""SRE activity feed for the demo (live or scripted).

Activity is thread-safe through its own small lock and never takes
rt.lock, so GET /api/demo/activity answers while a segment holds the
runtime lock.
"""

import threading
import time
from typing import Any


class Activity:
    """Running-segment state: who, what kind, and the turns so far."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "idle"
        self._incident: str | None = None
        self._deployment: str | None = None
        self._segment: str | None = None
        self._trigger: str | None = None
        self._model = ""
        self._tripwires: list[str] = []
        self._started_real: float | None = None
        self._call_in_flight = False
        self._turns: list[dict[str, Any]] = []

    def mark_running(
        self,
        *,
        incident: str,
        deployment: str,
        segment: str,
        trigger: str,
        model: str,
        tripwires: list[str] | None = None,
    ) -> None:
        """Mark a segment running, clearing the previous turn list."""
        with self._lock:
            self._state = "running"
            self._incident = incident
            self._deployment = deployment
            self._segment = segment
            self._trigger = trigger
            self._model = model
            self._tripwires = [str(item) for item in (tripwires or [])]
            self._started_real = time.time()
            self._call_in_flight = False
            self._turns = []

    def mark_idle(self) -> None:
        """Return to idle, clearing segment state."""
        with self._lock:
            self._state = "idle"
            self._incident = None
            self._deployment = None
            self._segment = None
            self._trigger = None
            self._model = ""
            self._tripwires = []
            self._started_real = None
            self._call_in_flight = False
            self._turns = []

    def record_turn(
        self, *, n: int, tool: Any, rationale: str, verdict: Any
    ) -> None:
        """Append one completed turn while a segment runs."""
        with self._lock:
            if self._state != "running":
                return
            self._turns.append({
                "n": n,
                "tool": tool,
                "rationale": rationale,
                "verdict": verdict,
            })

    def set_call_in_flight(self, value: bool) -> None:
        """Track whether a model call is currently in flight."""
        with self._lock:
            self._call_in_flight = bool(value)

    def snapshot(self) -> dict[str, Any]:
        """A JSON-able copy; takes only the activity lock."""
        with self._lock:
            if self._state == "running" and self._started_real is not None:
                elapsed = max(0.0, time.time() - self._started_real)
            else:
                elapsed = 0.0
            return {
                "state": self._state,
                "incident": self._incident,
                "deployment": self._deployment,
                "segment": self._segment,
                "trigger": self._trigger,
                "model": self._model,
                "tripwires": list(self._tripwires),
                "started_real": self._started_real,
                "elapsed_s": elapsed,
                "call_in_flight": self._call_in_flight,
                "turns": [dict(item) for item in self._turns],
            }


class ActivityInvestigator:
    """Wrap an Investigator so each segment marks activity running/idle.

    The pending trigger comes from the manager record at call time; the
    manager is attached after construction because it owns this wrapper.
    """

    def __init__(
        self,
        inner: Any,
        activity: Activity,
        model: str,
    ) -> None:
        self._inner = inner
        self._activity = activity
        self._model = model
        self._manager: Any = None

    @property
    def tools(self) -> Any:
        return self._inner.tools

    def set_manager(self, manager: Any) -> None:
        self._manager = manager

    def _record_for(self, incident_id: str) -> Any | None:
        manager = self._manager
        if manager is None:
            return None
        try:
            return manager.get(incident_id)
        except (ValueError, KeyError):
            return None

    def _trigger_for(self, incident_id: str, fallback: str) -> str:
        record = self._record_for(incident_id)
        if record is not None:
            pending = getattr(record, "pending", None)
            trigger = getattr(pending, "trigger", None)
            if isinstance(trigger, str) and trigger:
                return trigger
        return fallback

    def _tripwires_for(self, incident_id: str, incident: Any) -> list[str]:
        record = self._record_for(incident_id)
        if record is not None:
            tripwires = list(getattr(record, "tripwires", None) or [])
            if tripwires:
                return [str(item) for item in tripwires]
        single = getattr(incident, "tripwire", None)
        return [str(single)] if single else []

    def _run(
        self, kind: str, incident: Any, fallback: str, call: Any
    ) -> Any:
        incident_id = str(getattr(incident, "id", ""))
        deployment = str(getattr(incident, "deployment", ""))
        trigger = self._trigger_for(incident_id, fallback)
        self._activity.mark_running(
            incident=incident_id,
            deployment=deployment,
            segment=kind,
            trigger=trigger,
            model=self._model,
            tripwires=self._tripwires_for(incident_id, incident),
        )
        try:
            return call()
        finally:
            self._activity.mark_idle()

    def start(self, incident: Any) -> Any:
        return self._run(
            "start", incident, "opened",
            lambda: self._inner.start(incident),
        )

    def resume(
        self, incident: Any, *, message: str, extra_turns: int = 0
    ) -> Any:
        return self._run(
            "resume", incident, "reply",
            lambda: self._inner.resume(
                incident, message=message, extra_turns=extra_turns
            ),
        )

    def reopen(
        self, incident: Any, *, message: str, extra_turns: int = 0
    ) -> Any:
        return self._run(
            "reopen", incident, "rejection",
            lambda: self._inner.reopen(
                incident, message=message, extra_turns=extra_turns
            ),
        )


class ActivitySessionStore:
    """Wrap a SessionStore so each transcript append records a turn."""

    def __init__(self, inner: Any, activity: Activity) -> None:
        self._inner = inner
        self._activity = activity

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def save(self, session: Any) -> None:
        self._inner.save(session)

    def load(self, incident_id: str) -> Any:
        return self._inner.load(incident_id)

    def transcript(self, transcript_id: str) -> list[dict[str, Any]]:
        return self._inner.transcript(transcript_id)

    def append_transcript(
        self, transcript_id: str, record: dict[str, Any]
    ) -> None:
        self._inner.append_transcript(transcript_id, record)
        try:
            args = record.get("args")
            if isinstance(args, dict):
                rationale = str(args.get("rationale", "") or "")
            else:
                rationale = ""
            self._activity.record_turn(
                n=int(record.get("turn", 0)),
                tool=record.get("tool"),
                rationale=rationale,
                verdict=record.get("verdict"),
            )
        except (TypeError, ValueError):
            pass
