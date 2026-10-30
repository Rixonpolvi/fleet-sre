"""Mock Chronicle API authorization (mocks.md §4.2).

Permission names follow the documented pattern chronicle.<resource>.<verb>.
Local dev carries the identity in X-Fleetsre-Identity; deployed, a Google ID
token maps to an identity (D7).
"""

from fastapi import Request

from fleetsre.mock.world import ApiError

READER = "obs-reader"
DEPLOYER = "deployer"

IDENTITY_HEADER = "X-Fleetsre-Identity"

# Identity permission sets, exactly the mocks.md §4.2 table.
READER_PERMISSIONS = frozenset(
    {
        "chronicle.environments.get",
        "chronicle.environments.list",
        "chronicle.integrationInstances.get",
        "chronicle.integrationInstances.list",
        "chronicle.connectorInstances.get",
        "chronicle.connectorInstances.list",
        "chronicle.connectorInstanceLogs.get",
        "chronicle.connectorInstanceLogs.list",
        "chronicle.jobInstances.get",
        "chronicle.jobInstances.list",
        "chronicle.jobInstanceLogs.get",
        "chronicle.jobInstanceLogs.list",
    }
)

DEPLOYER_PERMISSIONS = READER_PERMISSIONS | frozenset(
    {
        "chronicle.connectorInstances.create",
        "chronicle.connectorInstances.update",
        "chronicle.jobInstances.update",
        "chronicle.jobInstances.run",
        "chronicle.environments.create",
    }
)

IDENTITY_PERMISSIONS = {
    READER: READER_PERMISSIONS,
    DEPLOYER: DEPLOYER_PERMISSIONS,
}

# Route -> required permission.
ROUTE_PERMISSIONS = {
    "environments.list": "chronicle.environments.list",
    "environments.get": "chronicle.environments.get",
    "environments.create": "chronicle.environments.create",
    "integrationInstances.list": "chronicle.integrationInstances.list",
    "integrationInstances.get": "chronicle.integrationInstances.get",
    # invented: allowed on .get (docs require .update) because it changes no
    # state and the read-only SRE needs it.
    "integrationInstances.executeTest": "chronicle.integrationInstances.get",
    "connectorInstances.list": "chronicle.connectorInstances.list",
    "connectorInstances.get": "chronicle.connectorInstances.get",
    "connectorInstances.create": "chronicle.connectorInstances.create",
    "connectorInstances.patch": "chronicle.connectorInstances.update",
    "connectorInstances.runOnDemand": "chronicle.connectorInstances.update",
    # invented: requires .update (docs require only .get) because enabling
    # clears existing logs.
    "connectorInstances.setLogsCollection": "chronicle.connectorInstances.update",
    "connectorInstanceLogs.list": "chronicle.connectorInstanceLogs.list",
    "connectorInstanceLogs.get": "chronicle.connectorInstanceLogs.get",
    "jobInstances.list": "chronicle.jobInstances.list",
    "jobInstances.get": "chronicle.jobInstances.get",
    "jobInstances.patch": "chronicle.jobInstances.update",
    "jobInstances.runOnDemand": "chronicle.jobInstances.run",
    "jobInstanceLogs.list": "chronicle.jobInstanceLogs.list",
    "jobInstanceLogs.get": "chronicle.jobInstanceLogs.get",
}


def resolve_identity(request: Request, dev_auth: bool) -> str:
    """Return the caller identity, or raise 401.

    Fails closed: without dev auth there is no verified identity until D7
    adds ID-token verification, so every request is unauthenticated.
    """
    identity = request.headers.get(IDENTITY_HEADER) if dev_auth else None
    if identity not in IDENTITY_PERMISSIONS:
        raise ApiError(401, "UNAUTHENTICATED", "missing or unknown identity")
    return identity


def require_permission(identity: str, permission: str) -> None:
    """Raise 403 naming the missing permission unless the identity has it."""
    if permission not in IDENTITY_PERMISSIONS[identity]:
        raise ApiError(
            403, "PERMISSION_DENIED", f"missing permission {permission}"
        )


def authorize(request: Request, dev_auth: bool, route: str) -> str:
    """Resolve the identity and enforce the route's required permission."""
    identity = resolve_identity(request, dev_auth)
    require_permission(identity, ROUTE_PERMISSIONS[route])
    return identity


def require_deployer(request: Request, dev_auth: bool) -> str:
    """Chaos routes: same auth as the API, deployer identity only."""
    identity = resolve_identity(request, dev_auth)
    if identity != DEPLOYER:
        raise ApiError(403, "PERMISSION_DENIED", "deployer identity required")
    return identity
