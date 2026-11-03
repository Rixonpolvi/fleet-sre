"""Demo web app (d8-pins 8-11). Admin-only local app with Jinja2 templates."""

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from jinja2 import Environment, FileSystemLoader

from fleetsre.web.views import fleet_view, incident_view, turn_input

TEMPLATES = Path(__file__).resolve().parent / "templates"

_COOKIE = "fleetsre_admin"
_HEADER = "x-fleetsre-admin"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES)), autoescape=True
    )


def _is_admin(request: Request, token: str) -> bool:
    candidates: list[str | None] = [
        request.headers.get("X-Fleetsre-Admin"),
        request.cookies.get(_COOKIE),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            if secrets.compare_digest(candidate, token):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _demo_view(host: Any) -> dict[str, Any]:
    rt = host.current
    last = rt.last_model_call_at
    transport = rt.transport
    names = sorted(host.acts.keys())
    return {
        "act": getattr(rt, "act_name", ""),
        "title": rt.act.title,
        "acts": names,
        "act_titles": {
            name: host.acts[name].title for name in names
        },
        "now": rt.clock.now().isoformat(),
        "started_at": rt.started_at.isoformat(),
        "speed": rt.speed,
        "held": bool(rt.held),
        "model_calls": rt.model_calls,
        "last_model_call_at": last.isoformat() if last else None,
        "active_faults": rt.active_faults(),
        "exhausted": bool(
            transport.exhausted if transport is not None else False
        ),
        "live": bool(getattr(host, "live", False)),
        "model": rt.gateway.config.roles["investigate"].model,
    }


def _version_view(host: Any) -> dict[str, str]:
    rt = host.current
    incidents = []
    for record in rt.manager.incidents():
        transcript_len = sum(
            len(rt.store.transcript(tid)) for tid in record.transcript_ids
        )
        state = (
            record.state.value
            if hasattr(record.state, "value") else str(record.state)
        )
        incidents.append({
            "id": record.id,
            "state": state,
            "timeline": len(record.timeline),
            "transcript": transcript_len,
        })
    incidents.sort(key=lambda item: item["id"])
    payload = {
        "act": getattr(rt, "act_name", ""),
        "held": bool(rt.held),
        "incidents": incidents,
        "status": rt.evaluator.status(),
        "model_calls": rt.model_calls,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {"version": hashlib.sha1(canonical).hexdigest()}


def create_app(
    host: Any, *, admin_token: str, operator: str = "operator"
) -> FastAPI:
    """Build the admin-only app: grid, incident, timeline, handoff (d8-pins 8)."""
    app = FastAPI()
    templates = _env()

    def _grid_page(request: Request) -> Response:
        query_token = request.query_params.get("admin")
        admin_error = False
        if query_token is not None:
            try:
                ok = secrets.compare_digest(query_token, admin_token)
            except (TypeError, ValueError):
                ok = False
            if ok:
                response = RedirectResponse(url="/", status_code=302)
                response.set_cookie(
                    _COOKIE, admin_token, httponly=True,
                    samesite="strict", path="/",
                )
                return response
            admin_error = True
        admin = _is_admin(request, admin_token)
        rt = host.current
        with rt.lock:
            fleet = fleet_view(rt)
            demo = _demo_view(host)
        html = templates.get_template("grid.html").render(
            fleet=fleet, demo=demo, admin=admin,
            admin_error=admin_error,
        )
        return HTMLResponse(html)

    def _incident_page(
        request: Request, incident_id: str
    ) -> Response:
        query_token = request.query_params.get("admin")
        admin_error = False
        if query_token is not None:
            try:
                ok = secrets.compare_digest(query_token, admin_token)
            except (TypeError, ValueError):
                ok = False
            if ok:
                response = RedirectResponse(
                    url=f"/incidents/{incident_id}", status_code=302
                )
                response.set_cookie(
                    _COOKIE, admin_token, httponly=True,
                    samesite="strict", path="/",
                )
                return response
            admin_error = True
        admin = _is_admin(request, admin_token)
        rt = host.current
        with rt.lock:
            try:
                view = incident_view(rt, incident_id)
            except (ValueError, KeyError):
                return HTMLResponse(
                    f"unknown incident: {incident_id}", status_code=404
                )
            demo = _demo_view(host)
            environment = view["deployment"].split("/", 1)[-1]
            rotatable = [
                fault for fault in rt.active_faults()
                if fault["type"] == "credential_expiry"
                and fault["environment"] == environment
            ]
        html = templates.get_template("incident.html").render(
            view=view, demo=demo, admin=admin, rotatable=rotatable,
            admin_error=admin_error,
        )
        return HTMLResponse(html)

    @app.get("/")
    def _get_grid(request: Request) -> Response:
        return _grid_page(request)

    @app.get("/api/fleet")
    def _get_fleet() -> dict[str, Any]:
        rt = host.current
        with rt.lock:
            return fleet_view(rt)

    @app.get("/incidents/{incident_id}")
    def _get_incident_page(
        request: Request, incident_id: str
    ) -> Response:
        return _incident_page(request, incident_id)

    @app.get("/api/incidents")
    def _list_incidents() -> list[dict[str, Any]]:
        rt = host.current
        with rt.lock:
            out = []
            for record in rt.manager.incidents():
                state = (
                    record.state.value
                    if hasattr(record.state, "value") else str(record.state)
                )
                out.append({
                    "id": record.id,
                    "deployment": record.incident.deployment,
                    "state": state,
                    "reason": record.reason,
                    "opened_at": record.incident.opened_at.isoformat(),
                })
            return out

    @app.get("/api/incidents/{incident_id}")
    def _get_incident(incident_id: str) -> Any:
        rt = host.current
        with rt.lock:
            try:
                return incident_view(rt, incident_id)
            except (ValueError, KeyError) as exc:
                return JSONResponse(
                    status_code=404, content={"error": str(exc)}
                )

    @app.get("/api/incidents/{incident_id}/turns/{n}/input")
    def _get_turn_input(incident_id: str, n: int) -> Any:
        rt = host.current
        with rt.lock:
            try:
                return turn_input(rt, incident_id, n)
            except (ValueError, KeyError) as exc:
                return JSONResponse(
                    status_code=404, content={"error": str(exc)}
                )

    @app.get("/api/demo")
    def _get_demo() -> dict[str, Any]:
        rt = host.current
        with rt.lock:
            return _demo_view(host)

    @app.get("/api/version")
    def _get_version() -> dict[str, str]:
        rt = host.current
        with rt.lock:
            return _version_view(host)

    def _forbidden() -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error": "admin credential required"},
        )

    def _manager_error(exc: Exception) -> JSONResponse:
        if isinstance(exc, KeyError) or (
            "unknown incident" in str(exc).lower()
        ):
            return JSONResponse(
                status_code=404, content={"error": str(exc)}
            )
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.post("/api/incidents/{incident_id}/approve")
    async def _approve(incident_id: str, request: Request) -> Any:
        if not _is_admin(request, admin_token):
            return _forbidden()
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        rt = host.current
        with rt.lock:
            try:
                rt.manager.approve(
                    incident_id, str(body.get("proposal_hash", "")),
                    by=operator,
                )
            except (ValueError, KeyError) as exc:
                return _manager_error(exc)
            return incident_view(rt, incident_id)

    @app.post("/api/incidents/{incident_id}/reject")
    async def _reject(incident_id: str, request: Request) -> Any:
        if not _is_admin(request, admin_token):
            return _forbidden()
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        rt = host.current
        with rt.lock:
            try:
                rt.manager.reject(
                    incident_id, str(body.get("proposal_hash", "")),
                    by=operator, notes=str(body.get("notes", "")),
                )
            except (ValueError, KeyError) as exc:
                return _manager_error(exc)
            return incident_view(rt, incident_id)

    @app.post("/api/incidents/{incident_id}/reply")
    async def _reply(incident_id: str, request: Request) -> Any:
        if not _is_admin(request, admin_token):
            return _forbidden()
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        try:
            extra = int(body.get("extra_turns", 0) or 0)
        except (TypeError, ValueError):
            extra = 0
        rt = host.current
        with rt.lock:
            try:
                rt.manager.reply(
                    incident_id, str(body.get("message", "")),
                    by=operator, extra_turns=extra,
                )
            except (ValueError, KeyError) as exc:
                return _manager_error(exc)
            return incident_view(rt, incident_id)

    @app.post("/api/incidents/{incident_id}/grant")
    async def _grant(incident_id: str, request: Request) -> Any:
        if not _is_admin(request, admin_token):
            return _forbidden()
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        try:
            turns = int(body.get("turns", 0))
        except (TypeError, ValueError):
            turns = 0
        if turns < 1 or turns > 24:
            return JSONResponse(
                status_code=409,
                content={"error": f"turns must be 1-24, got {turns}"},
            )
        rt = host.current
        with rt.lock:
            try:
                rt.manager.reply(
                    incident_id, f"Granted {turns} more turns.",
                    by=operator, extra_turns=turns,
                )
            except (ValueError, KeyError) as exc:
                return _manager_error(exc)
            return incident_view(rt, incident_id)

    @app.post("/api/incidents/{incident_id}/close")
    async def _close(incident_id: str, request: Request) -> Any:
        if not _is_admin(request, admin_token):
            return _forbidden()
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        rt = host.current
        with rt.lock:
            try:
                rt.manager.close(
                    incident_id, by=operator,
                    note=str(body.get("note", "")),
                )
            except (ValueError, KeyError) as exc:
                return _manager_error(exc)
            return incident_view(rt, incident_id)

    @app.post("/api/demo/faults/{fault_id}/clear")
    def _clear_fault(fault_id: str, request: Request) -> Any:
        if not _is_admin(request, admin_token):
            return _forbidden()
        rt = host.current
        with rt.lock:
            known = {fault["id"] for fault in rt.active_faults()}
            if fault_id not in known:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"unknown fault: {fault_id}"},
                )
            rt.clear_fault(fault_id, by=operator)
            return _demo_view(host)

    @app.post("/api/demo/act")
    async def _switch_act(request: Request) -> Any:
        if not _is_admin(request, admin_token):
            return _forbidden()
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        name = str(body.get("act", ""))
        if name not in host.acts:
            return JSONResponse(
                status_code=404, content={"error": f"unknown act: {name}"}
            )
        host.switch(name)
        rt = host.current
        with rt.lock:
            return _demo_view(host)

    return app
