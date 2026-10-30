"""Mock Chronicle API: control plane over the world (mocks.md §4).

Google-style REST: base /v1/, resource names in the path, custom methods as
POST /v1/{name}:method, PATCH with updateMask, list with pageSize/pageToken.
Errors use Google's shape: {"error": {"code", "message", "status"}}.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from fleetsre.mock.auth import authorize, require_deployer
from fleetsre.mock.world import ApiError, FaultSpec, World

_STATUS_BY_CODE = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    500: "INTERNAL",
}


def _google_error(code: int, message: str, status: str) -> JSONResponse:
    return JSONResponse(
        status_code=code,
        content={"error": {"code": code, "message": message, "status": status}},
    )


def _page_size(params: Any) -> int | None:
    raw = params.get("pageSize")
    if raw is None:
        return None
    try:
        size = int(raw)
    except (TypeError, ValueError):
        raise ApiError(400, "INVALID_ARGUMENT", "invalid pageSize") from None
    if size < 0:
        raise ApiError(400, "INVALID_ARGUMENT", "invalid pageSize")
    return size


def create_app(
    world: World, *, chaos: bool = False, dev_auth: bool = False
) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _google_error(exc.code, exc.message, exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        status = _STATUS_BY_CODE.get(exc.status_code, "UNKNOWN")
        return _google_error(exc.status_code, str(exc.detail), status)

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _google_error(400, str(exc), "INVALID_ARGUMENT")

    @app.exception_handler(ValidationError)
    async def _body_validation_error(
        _: Request, exc: ValidationError
    ) -> JSONResponse:
        return _google_error(400, str(exc), "INVALID_ARGUMENT")

    prefix = world.prefix

    # -- environments ---------------------------------------------------

    @app.get(f"/v1/{prefix}/environments")
    def _list_envs(request: Request) -> dict[str, Any]:
        authorize(request, dev_auth, "environments.list")
        return {"environments": world.list_environments()}

    @app.get(f"/v1/{prefix}/environments/{{env_id}}")
    def _get_env(request: Request, env_id: str) -> dict[str, Any]:
        authorize(request, dev_auth, "environments.get")
        return world.get_environment(env_id)

    @app.post(f"/v1/{prefix}/environments")
    async def _create_env(request: Request) -> dict[str, Any]:
        authorize(request, dev_auth, "environments.create")
        body = await request.json()
        return world.create_environment(body)

    # -- integration instances ------------------------------------------

    @app.get(
        f"/v1/{prefix}/integrations/{{integration}}/integrationInstances"
    )
    def _list_integs(request: Request, integration: str) -> dict[str, Any]:
        authorize(request, dev_auth, "integrationInstances.list")
        return {
            "integrationInstances": world.list_integration_instances(integration)
        }

    @app.get(
        f"/v1/{prefix}/integrations/{{integration}}"
        "/integrationInstances/{instance}"
    )
    def _get_integ(
        request: Request, integration: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "integrationInstances.get")
        name = (
            f"{prefix}/integrations/{integration}/"
            f"integrationInstances/{instance}"
        )
        return world.get_integration_instance(name)

    @app.post(
        f"/v1/{prefix}/integrations/{{integration}}"
        "/integrationInstances/{instance}:executeTest"
    )
    def _exec_test(
        request: Request, integration: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "integrationInstances.executeTest")
        name = (
            f"{prefix}/integrations/{integration}/"
            f"integrationInstances/{instance}"
        )
        return world.execute_test(name)

    # -- connector instances --------------------------------------------

    @app.get(
        f"/v1/{prefix}/integrations/{{integration}}/connectors/{{connector}}"
        "/connectorInstances"
    )
    def _list_conns(
        request: Request, integration: str, connector: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "connectorInstances.list")
        parent = (
            f"{prefix}/integrations/{integration}/connectors/{connector}"
        )
        return {"connectorInstances": world.list_connector_instances(parent)}

    @app.post(
        f"/v1/{prefix}/integrations/{{integration}}/connectors/{{connector}}"
        "/connectorInstances"
    )
    async def _create_conn(
        request: Request, integration: str, connector: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "connectorInstances.create")
        parent = (
            f"{prefix}/integrations/{integration}/connectors/{connector}"
        )
        body = await request.json()
        return world.create_connector_instance(parent, body)

    def _conn_name(integration: str, connector: str, instance: str) -> str:
        return (
            f"{prefix}/integrations/{integration}/connectors/{connector}"
            f"/connectorInstances/{instance}"
        )

    @app.get(
        f"/v1/{prefix}/integrations/{{integration}}/connectors/{{connector}}"
        "/connectorInstances/{instance}"
    )
    def _get_conn(
        request: Request, integration: str, connector: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "connectorInstances.get")
        return world.get_connector(_conn_name(integration, connector, instance))

    @app.patch(
        f"/v1/{prefix}/integrations/{{integration}}/connectors/{{connector}}"
        "/connectorInstances/{instance}"
    )
    async def _patch_conn(
        request: Request, integration: str, connector: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "connectorInstances.patch")
        body = await request.json()
        mask = request.query_params.get("updateMask", "").split(",")
        return world.patch_connector(
            _conn_name(integration, connector, instance), mask, body
        )

    @app.post(
        f"/v1/{prefix}/integrations/{{integration}}/connectors/{{connector}}"
        "/connectorInstances/{instance}:runOnDemand"
    )
    async def _conn_run(
        request: Request, integration: str, connector: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "connectorInstances.runOnDemand")
        body = await request.json()
        return world.run_connector_on_demand(
            _conn_name(integration, connector, instance), body
        )

    @app.post(
        f"/v1/{prefix}/integrations/{{integration}}/connectors/{{connector}}"
        "/connectorInstances/{instance}:setLogsCollection"
    )
    async def _conn_logs_on(
        request: Request, integration: str, connector: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "connectorInstances.setLogsCollection")
        body = await request.json()
        return world.set_logs_collection(
            _conn_name(integration, connector, instance), body
        )

    @app.get(
        f"/v1/{prefix}/integrations/{{integration}}/connectors/{{connector}}"
        "/connectorInstances/{instance}/logs"
    )
    def _list_conn_logs(
        request: Request, integration: str, connector: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "connectorInstanceLogs.list")
        params = request.query_params
        return world.list_connector_logs(
            _conn_name(integration, connector, instance),
            _page_size(params),
            params.get("pageToken"),
        )

    @app.get(
        f"/v1/{prefix}/integrations/{{integration}}/connectors/{{connector}}"
        "/connectorInstances/{instance}/logs/{log}"
    )
    def _get_conn_log(
        request: Request, integration: str, connector: str, instance: str,
        log: str,
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "connectorInstanceLogs.get")
        return world.get_connector_log(
            _conn_name(integration, connector, instance), log
        )

    # -- job instances ---------------------------------------------------

    @app.get(
        f"/v1/{prefix}/integrations/{{integration}}/jobs/{{job}}/jobInstances"
    )
    def _list_jobs(
        request: Request, integration: str, job: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "jobInstances.list")
        parent = f"{prefix}/integrations/{integration}/jobs/{job}"
        return {"jobInstances": world.list_job_instances(parent)}

    def _job_name(integration: str, job: str, instance: str) -> str:
        return (
            f"{prefix}/integrations/{integration}/jobs/{job}"
            f"/jobInstances/{instance}"
        )

    @app.get(
        f"/v1/{prefix}/integrations/{{integration}}/jobs/{{job}}"
        "/jobInstances/{instance}"
    )
    def _get_job(
        request: Request, integration: str, job: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "jobInstances.get")
        return world.get_job(_job_name(integration, job, instance))

    @app.patch(
        f"/v1/{prefix}/integrations/{{integration}}/jobs/{{job}}"
        "/jobInstances/{instance}"
    )
    async def _patch_job(
        request: Request, integration: str, job: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "jobInstances.patch")
        body = await request.json()
        mask = request.query_params.get("updateMask", "").split(",")
        return world.patch_job(_job_name(integration, job, instance), mask, body)

    @app.post(
        f"/v1/{prefix}/integrations/{{integration}}/jobs/{{job}}"
        "/jobInstances/{instance}:runOnDemand"
    )
    def _job_run(
        request: Request, integration: str, job: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "jobInstances.runOnDemand")
        return world.run_job_on_demand(_job_name(integration, job, instance))

    @app.get(
        f"/v1/{prefix}/integrations/{{integration}}/jobs/{{job}}"
        "/jobInstances/{instance}/logs"
    )
    def _list_job_logs(
        request: Request, integration: str, job: str, instance: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "jobInstanceLogs.list")
        params = request.query_params
        return world.list_job_logs(
            _job_name(integration, job, instance),
            _page_size(params),
            params.get("pageToken"),
        )

    @app.get(
        f"/v1/{prefix}/integrations/{{integration}}/jobs/{{job}}"
        "/jobInstances/{instance}/logs/{log}"
    )
    def _get_job_log(
        request: Request, integration: str, job: str, instance: str, log: str
    ) -> dict[str, Any]:
        authorize(request, dev_auth, "jobInstanceLogs.get")
        return world.get_job_log(_job_name(integration, job, instance), log)

    # -- chaos -----------------------------------------------------------
    if chaos:
        @app.post("/_chaos/faults")
        async def _chaos_inject(request: Request) -> dict[str, Any]:
            require_deployer(request, dev_auth)
            body = await request.json()
            fid = world.inject(FaultSpec(**body))
            return {"id": fid}

        @app.get("/_chaos/faults")
        def _chaos_list(request: Request) -> dict[str, Any]:
            require_deployer(request, dev_auth)
            return {"faults": world.list_faults()}

        @app.delete("/_chaos/faults/{fault_id}")
        def _chaos_clear(request: Request, fault_id: str) -> dict[str, Any]:
            require_deployer(request, dev_auth)
            world.clear(fault_id)
            return {"id": fault_id}

        @app.post("/_chaos/schedule")
        async def _chaos_schedule(request: Request) -> dict[str, Any]:
            require_deployer(request, dev_auth)
            body = await request.json()
            count = world.schedule(body)
            return {"scheduled": count}

    return app
