"""Authoring schema (d6b-pins 3). Stub: everything raises."""

from typing import Any


def flatten_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local $refs and drop meta keys (d6b-pins 3)."""
    raise NotImplementedError


def authoring_schema() -> dict[str, Any]:
    """Flattened contract schema minus provenance and _-keys (d6b-pins 3)."""
    raise NotImplementedError
