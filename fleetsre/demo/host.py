"""Multi-act demo host (D8 stub; Session B implements per d8-pins 5)."""

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
    ) -> None:
        raise NotImplementedError("D8 Session B implements DemoHost")

    def switch(self, name: str) -> None:
        """Build and start a fresh runtime for the named act (d8-pins 5)."""
        raise NotImplementedError("D8 Session B implements DemoHost")
