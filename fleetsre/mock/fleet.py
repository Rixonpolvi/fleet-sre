"""D1 fleet stub: demo MSSP fleet config (mocks.md §2)."""

from pathlib import Path
from typing import Any

import yaml
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
    """Load and validate the fleet YAML file as it is."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return FleetConfig.model_validate(data)
