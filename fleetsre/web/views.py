"""Pure view functions for the demo web app (D8 stub, d8-pins 8, 12-15)."""

from typing import Any


def fleet_view(rt: Any) -> dict[str, Any]:
    """Grid data: act, now, held, services x deployments (d8-pins 12)."""
    raise NotImplementedError("D8 Session B implements fleet_view")


def incident_view(rt: Any, incident_id: str) -> dict[str, Any]:
    """Incident data: hypotheses, evidence, proposal, turns, handoff (13-15)."""
    raise NotImplementedError("D8 Session B implements incident_view")


def turn_input(rt: Any, incident_id: str, turn: int) -> list[dict[str, Any]]:
    """Exact model input of turn n, encrypted_content elided (d8-pins 14)."""
    raise NotImplementedError("D8 Session B implements turn_input")
