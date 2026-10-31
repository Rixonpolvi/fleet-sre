"""D4 registry: Deployment data model (spec.md §4.1). Data model only."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Deployment:
    """A service applied to one environment, with parameters from fleet.yaml."""

    id: str
    service: str
    environment: str
    parameters: dict[str, Any]
