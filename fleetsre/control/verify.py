"""Change-point verifier (d6a-pins 6, 15). Status-based, no model calls."""

from datetime import timedelta
from typing import Any, Literal


class Verifier:
    """Tracks one verification window per incident (pin 15)."""

    def __init__(
        self, evaluator: Any, registry: Any, clock: Any
    ) -> None:
        self._evaluator = evaluator
        self._registry = registry
        self._clock = clock
        self._windows: dict[str, dict[str, Any]] = {}

    def start(
        self,
        incident: Any,
        diagnosis: Any,
        *,
        changed_resource: str | None,
    ) -> None:
        if hasattr(diagnosis, "model_dump"):
            diagnosis = diagnosis.model_dump(mode="json")
        expected = diagnosis["expectedVerification"]
        now = self._clock.now()
        seconds = int(expected["seconds"])
        deployment = incident.deployment
        self._windows[incident.id] = {
            "deployment": deployment,
            "tripwire": expected["tripwire"],
            "seconds": seconds,
            "started": now,
            "deadline": now + timedelta(seconds=seconds),
        }
        if changed_resource is not None:
            names = self._registry.deployment(deployment).resource_names
            component = next(
                comp for comp, name in names.items()
                if name == changed_resource
            )
            contract = self._registry.contract(incident.service)
            self._evaluator.enter_change_point(
                deployment,
                component,
                duration_s=contract.changePoint.durationSeconds,
                overrides=contract.changePoint.overrides,
            )

    def check(self, incident_id: str) -> Literal["pending", "verified", "failed"]:
        window = self._windows[incident_id]
        status = self._evaluator.status()[
            window["deployment"]
        ][window["tripwire"]]
        if status == "ok":
            del self._windows[incident_id]
            return "verified"
        if self._clock.now() >= window["deadline"]:
            del self._windows[incident_id]
            return "failed"
        return "pending"

    def active(self) -> list[str]:
        return list(self._windows)
