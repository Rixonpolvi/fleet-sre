"""Scripted demo transport (d8-pins 3). Wraps ScriptedTransport."""

import copy
import time
from typing import Any

from fleetsre.sre.gateway import ScriptedTransport, TransportError


class DemoTransport:
    """Wraps ScriptedTransport; sets exhausted and raises TransportError(400)
    once the script is used up. `calls` counts every create."""

    def __init__(
        self, responses: list[dict[str, Any]], *, turn_delay_s: float = 0.0
    ) -> None:
        self._inner = ScriptedTransport(list(responses))
        self._script_length = len(responses)
        self.exhausted = False
        self.calls = 0
        self._turn_delay_s = float(turn_delay_s)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._inner.requests

    @property
    def timeouts(self) -> list[float]:
        return self._inner.timeouts

    @property
    def script_length(self) -> int:
        return self._script_length

    def create(
        self, request: dict[str, Any], *, timeout: float = 120.0
    ) -> dict[str, Any]:
        """One model call; records the request like ScriptedTransport."""
        self.calls += 1
        try:
            response = self._inner.create(request, timeout=timeout)
        except IndexError:
            self.exhausted = True
            raise TransportError(400, "demo script exhausted") from None
        if self._turn_delay_s > 0:
            time.sleep(self._turn_delay_s)
        return response

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy({
            "calls": self.calls,
            "exhausted": self.exhausted,
        })
