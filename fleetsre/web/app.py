"""Demo web app (D8 stub; Session B implements per d8-pins 8-11)."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app(
    host: Any, *, admin_token: str, operator: str = "operator"
) -> "FastAPI":
    """Build the admin-only app: grid, incident, timeline, handoff (d8-pins 8)."""
    raise NotImplementedError("D8 Session B implements create_app")
