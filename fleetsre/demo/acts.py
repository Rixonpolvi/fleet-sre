"""Demo act specs (d8-pins 1-2). Mirrors fixtures/demo/acts.yaml."""

from datetime import datetime
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator


class ActFault(BaseModel):
    """One scheduled fault, passed to world.schedule as in D6 E2E."""

    model_config = ConfigDict(extra="forbid")

    at: int
    type: str
    environment: str
    target: str | None = None
    params: dict[str, Any] = {}
    clearAt: int | None = None

    def schedule_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "at": self.at,
            "type": self.type,
            "environment": self.environment,
            "params": dict(self.params),
        }
        if self.target is not None:
            item["target"] = self.target
        if self.clearAt is not None:
            item["clearAt"] = self.clearAt
        return item


class ActAction(BaseModel):
    """One --check action: what the human does in the video (d8-pins 1)."""

    model_config = ConfigDict(extra="forbid")

    on: str
    do: str
    notes: str | None = None


class ActSpec(BaseModel):
    """One act from fixtures/demo/acts.yaml (d8-pins 1), extra="forbid"."""

    model_config = ConfigDict(extra="forbid")

    title: str
    seed: int
    t0: datetime
    tick_seconds: int
    faults: list[ActFault]
    responses: list[str]
    incident: str
    actions: list[ActAction] = []

    @field_validator("t0", mode="before")
    @classmethod
    def _parse_t0(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text)
        return value


def load_acts(path: str = "fixtures/demo/acts.yaml") -> dict[str, ActSpec]:
    """Load the act specs keyed F1, F2, F3 (d8-pins 2)."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"acts file {path} must hold a mapping")
    out: dict[str, ActSpec] = {}
    for name, spec in raw.items():
        if isinstance(spec, dict) and isinstance(spec.get("actions"), list):
            for action in spec["actions"]:
                # YAML 1.1 parses the `on:` key as boolean True.
                if isinstance(action, dict) and True in action:
                    action["on"] = action.pop(True)
        out[name] = ActSpec.model_validate(spec)
    return out
