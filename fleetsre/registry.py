"""D4 registry: Deployment data model (spec.md §4.1). Data model only."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from fleetsre.logmodel import LogEntry

if TYPE_CHECKING:
    from fleetsre.contracts import Contract
    from fleetsre.mock.fleet import FleetConfig


@dataclass(frozen=True)
class Deployment:
    """A service applied to one environment, with parameters from fleet.yaml."""

    id: str
    service: str
    environment: str
    parameters: dict[str, Any]
    resources: Mapping[str, str] = field(default_factory=dict)


def deployments_from_fleet(
    fleet: "FleetConfig",
    contract: "Contract",
    environments: Sequence[str] | None = None,
) -> list[Deployment]:
    """Build armed deployments for a service from a loaded fleet.

    Parameters come from the fleet's deployment entries. ``resources``
    maps component id to the fleet's resource instance id, matched by
    component kind (connectorInstance -> connectorInstance,
    jobInstance -> jobInstance); components without a fleet instance id
    (e.g. ETL flows) have no entry.
    """
    if environments is None:
        envs = sorted(
            env
            for env, spec in fleet.environments.items()
            if contract.service in spec.deployments
        )
    else:
        envs = list(environments)
    deployments: list[Deployment] = []
    for env in envs:
        dep = fleet.environments[env].deployments[contract.service]
        resources: dict[str, str] = {}
        for component in contract.components:
            if (
                component.kind == "connectorInstance"
                and dep.connectorInstance
            ):
                resources[component.id] = dep.connectorInstance
            elif component.kind == "jobInstance" and dep.jobInstance:
                resources[component.id] = dep.jobInstance
        deployments.append(
            Deployment(
                id=f"{contract.service}/{env}",
                service=contract.service,
                environment=env,
                parameters=dict(dep.parameters),
                resources=resources,
            )
        )
    return deployments


class Registry:
    """D5 stub: contracts + deployments + scorecards with log routing."""

    def __init__(
        self,
        contracts: list["Contract"],
        deployments: list[Deployment],
        scorecards: dict[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError

    def contract(self, service: str) -> "Contract":
        raise NotImplementedError

    def deployments(self, service: str) -> list[Deployment]:
        raise NotImplementedError

    def deployment(self, id: str) -> Deployment:
        raise NotImplementedError

    def scorecard(self, service: str) -> Any:
        raise NotImplementedError

    def route(self, entry: LogEntry) -> list[tuple[str, str]]:
        raise NotImplementedError
