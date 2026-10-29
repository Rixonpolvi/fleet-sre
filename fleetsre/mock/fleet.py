"""D1 fleet stub: demo MSSP fleet config (mocks.md §2)."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    parameters: dict[str, Any] = {}


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    displayName: str = ""
    deployments: dict[str, DeploymentConfig] = {}


class FleetConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    instance: str = ""
    environments: dict[str, EnvironmentConfig] = {}


def load_fleet(path: str | Path) -> FleetConfig:
    """Load and validate the fleet file. Session B implements."""
    raise NotImplementedError
