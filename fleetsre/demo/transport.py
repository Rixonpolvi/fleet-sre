"""Scripted demo transport (D8 stub; Session B implements per d8-pins 3)."""

from typing import Any


class DemoTransport:
    """Wraps ScriptedTransport; sets exhausted and raises TransportError(400)
    once the script is used up. `calls` counts every create."""

    def __init__(
        self, responses: list[dict[str, Any]], *, timeout: float = 120.0
    ) -> None:
        raise NotImplementedError("D8 Session B implements DemoTransport")

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        """One model call; records the request like ScriptedTransport."""
        raise NotImplementedError("D8 Session B implements DemoTransport")
