"""Demo act specs (D8 stub; Session B implements per d8-pins 1-2)."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class ActSpec(BaseModel):
    """One act from fixtures/demo/acts.yaml (d8-pins 1), extra="forbid"."""

    model_config = ConfigDict(extra="forbid")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("D8 Session B implements ActSpec")


def load_acts(path: str = "fixtures/demo/acts.yaml") -> dict[str, ActSpec]:
    """Load the act specs keyed F1, F2, F3 (d8-pins 2)."""
    raise NotImplementedError("D8 Session B implements load_acts")
