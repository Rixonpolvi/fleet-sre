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
    resource_names: Mapping[str, str] = field(default_factory=dict)


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
    connectors: dict[str, str] = {}
    jobs: dict[str, str] = {}
    for integ_name, spec in fleet.integrations.items():
        if not isinstance(spec, dict):
            continue
        for key in spec.get("connectors", {}):
            connectors.setdefault(integ_name, key)
        for key in spec.get("jobs", {}):
            jobs.setdefault(integ_name, key)
    deployments: list[Deployment] = []
    for env in envs:
        dep = fleet.environments[env].deployments[contract.service]
        resources: dict[str, str] = {}
        resource_names: dict[str, str] = {}
        used_integration: str | None = None
        for component in contract.components:
            if (
                component.kind == "connectorInstance"
                and dep.connectorInstance
                and connectors
            ):
                resources[component.id] = dep.connectorInstance
                integ = next(iter(connectors))
                used_integration = integ
                resource_names[component.id] = (
                    f"{fleet.instance}/integrations/{integ}/connectors/"
                    f"{connectors[integ]}/connectorInstances/"
                    f"{dep.connectorInstance}"
                )
            elif component.kind == "jobInstance" and dep.jobInstance and jobs:
                resources[component.id] = dep.jobInstance
                integ = next(iter(jobs))
                used_integration = integ
                resource_names[component.id] = (
                    f"{fleet.instance}/integrations/{integ}/jobs/"
                    f"{jobs[integ]}/jobInstances/{dep.jobInstance}"
                )
        if used_integration is not None:
            resource_names["integrationInstance"] = (
                f"{fleet.instance}/integrations/{used_integration}/"
                f"integrationInstances/{env}"
            )
        deployments.append(
            Deployment(
                id=f"{contract.service}/{env}",
                service=contract.service,
                environment=env,
                parameters=dict(dep.parameters),
                resources=resources,
                resource_names=resource_names,
            )
        )
    return deployments


# Fixed Chronicle resource prefix (docs/mocks.md §4.1). Full resource names
# come from the fleet via Deployment.resource_names, never from copies here.
CHRONICLE_PREFIX = "projects/fleet-sre-demo/locations/us/instances/mock-soar"


class Registry:
    """Contracts + deployments + scorecards with log routing (spec.md §4.1)."""

    def __init__(
        self,
        contracts: list["Contract"],
        deployments: list[Deployment],
        scorecards: dict[str, Any] | None = None,
    ) -> None:
        self._contracts = {c.service: c for c in contracts}
        self._deployments = {d.id: d for d in deployments}
        self._by_service: dict[str, list[Deployment]] = {}
        for dep in deployments:
            self._by_service.setdefault(dep.service, []).append(dep)
        self._scorecards = dict(scorecards) if scorecards else {}

    @property
    def services(self) -> tuple[str, ...]:
        return tuple(self._contracts)

    def contract(self, service: str) -> "Contract":
        return self._contracts[service]

    def deployments(self, service: str) -> list[Deployment]:
        return list(self._by_service.get(service, []))

    def deployment(self, id: str) -> Deployment:
        return self._deployments[id]

    def scorecard(self, service: str) -> Any:
        return self._scorecards.get(service)

    def route(self, entry: LogEntry) -> list[tuple[str, str]]:
        """Route an entry to [(deployment ID, component ID)].

        Uses the evaluator's routing (deliverables.md §D4 item 5):
        the component's container_name and match labels, then
        deploymentBy; a connector label maps through
        Deployment.resources, with jsonPayload.environment as fallback.
        """
        found: list[tuple[str, str]] = []
        container = entry.resource.labels.get("container_name")
        for service, contract in self._contracts.items():
            for component in contract.components:
                if component.logs.container_name != container:
                    continue
                if any(
                    key not in entry.labels
                    for key in component.logs.match
                ):
                    continue
                dep_id = self._resolve(
                    service, component.id, component.logs.deploymentBy,
                    entry,
                )
                if dep_id is not None:
                    found.append((dep_id, component.id))
        return found

    def _resolve(
        self, service: str, component_id: str, deployment_by: str,
        entry: LogEntry,
    ) -> str | None:
        if deployment_by.startswith("labels."):
            value = entry.labels.get(deployment_by[len("labels."):])
            if value is not None:
                for dep in self._by_service.get(service, []):
                    if dep.resources.get(component_id) == value:
                        return dep.id
            return self._env_fallback(service, entry)
        if deployment_by.startswith("jsonPayload."):
            node: Any = entry.jsonPayload
            for part in deployment_by.split(".")[1:]:
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return None
            if not isinstance(node, str):
                return None
            dep_id = f"{service}/{node}"
            return dep_id if dep_id in self._deployments else None
        return None

    def _env_fallback(self, service: str, entry: LogEntry) -> str | None:
        env = entry.jsonPayload.get("environment")
        if not isinstance(env, str):
            return None
        dep_id = f"{service}/{env}"
        return dep_id if dep_id in self._deployments else None
