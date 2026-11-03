"""Single-act demo runtime (D8 stub; Session B implements per d8-pins 4)."""

from threading import Event
from typing import Any


class DemoRuntime:
    """A replica of one act's D6 end-to-end world (d8-pins 4).

    Members: act, clock, world, sink, evaluator, registry, manager,
    store, loop, transport (DemoTransport, or None when live), lock,
    started_at, held, model_calls, last_model_call_at.
    """

    def __init__(
        self,
        act: Any,
        *,
        live: bool = False,
        speed: float = 10.0,
        lead_s: int = 60,
    ) -> None:
        raise NotImplementedError("D8 Session B implements DemoRuntime")

    def start(self) -> None:
        """Step until first_fault_at - lead_s; record started_at."""
        raise NotImplementedError("D8 Session B implements DemoRuntime")

    def advance(self, seconds: float) -> None:
        """Step until the clock has moved `seconds` (d8-pins 4, 9)."""
        raise NotImplementedError("D8 Session B implements DemoRuntime")

    def run_realtime(self, stop: Event) -> None:
        """Advance one tick every tick_seconds / speed real seconds."""
        raise NotImplementedError("D8 Session B implements DemoRuntime")

    def active_faults(self) -> list[dict[str, Any]]:
        """The world's active faults: id, type, environment."""
        raise NotImplementedError("D8 Session B implements DemoRuntime")

    def clear_fault(self, fault_id: str) -> None:
        """Clear the fault and append the demo timeline event (d8-pins 4)."""
        raise NotImplementedError("D8 Session B implements DemoRuntime")
