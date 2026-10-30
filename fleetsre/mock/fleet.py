"""Fleet config: demo MSSP tenants on one mock SOAR instance (mocks.md §2)."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict


class DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    parameters: dict[str, Any] = {}
    connectorInstance: str | None = None
    jobInstance: str | None = None


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    displayName: str = ""
    description: str = ""
    contactEmails: list[str] = []
    credentials: dict[str, str] = {}
    deployments: dict[str, DeploymentConfig] = {}


class FleetConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int = 1
    instance: str = ""
    integrations: dict[str, Any] = {}
    environments: dict[str, EnvironmentConfig] = {}


def load_fleet(path: str | Path) -> FleetConfig:
    """Load and validate the fleet YAML file as it is."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return FleetConfig.model_validate(data)
