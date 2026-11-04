"""Multi-act demo host (d8-pins 5). Holds one DemoRuntime at a time."""

import threading
from typing import Any


class DemoHost:
    """Holds one DemoRuntime; switch(name) builds and starts a fresh one."""

    def __init__(
        self,
        acts: dict[str, Any],
        *,
        live: bool = False,
        speed: float = 10.0,
        realtime: bool = False,
        turn_delay_s: float = 0.0,
    ) -> None:
        from fleetsre.demo.runtime import DemoRuntime  # noqa: F401

        self.acts = acts
        self.live = live
        self.speed = speed
        self.realtime = realtime
        self.turn_delay_s = float(turn_delay_s)
        self.current: Any = None
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def switch(self, name: str) -> None:
        """Build and start a fresh runtime for the named act (d8-pins 5)."""
        from fleetsre.demo.runtime import DemoRuntime

        if name not in self.acts:
            raise ValueError(f"unknown act: {name}")
        self._stop_thread()
        runtime = DemoRuntime(
            self.acts[name], live=self.live, speed=self.speed,
            turn_delay_s=self.turn_delay_s,
        )
        runtime.act_name = name
        runtime.start()
        self.current = runtime
        if self.realtime:
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=runtime.run_realtime,
                args=(self._stop,),
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the realtime thread, if one is running."""
        self._stop_thread()

    def _stop_thread(self) -> None:
        if self._thread is not None:
            assert self._stop is not None
            self._stop.set()
            self._thread.join(timeout=5.0)
            self._thread = None
            self._stop = None
