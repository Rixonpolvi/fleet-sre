"""D3 review fixes: unit tests for items 1-6."""

from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from fleetsre.clock import SimClock
from fleetsre.mock.auth import (
    DEPLOYER_PERMISSIONS,
    IDENTITY_PERMISSIONS,
    READER_PERMISSIONS,
    ROUTE_PERMISSIONS,
)
from fleetsre.mock.chronicle_api import create_app
from fleetsre.mock.fleet import load_fleet
from fleetsre.mock.world import ApiError, FaultSpec, World
from fleetsre.ports import MemorySink

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)
PROJECT = "fleet-sre-test"

_DEPLOYER = {"X-Fleetsre-Identity": "deployer"}
_READER = {"X-Fleetsre-Identity": "obs-reader"}


def _fleet_doc() -> dict:
    return yaml.safe_load((ROOT / "fixtures" / "fleet.yaml").read_text())


_FLEET = _fleet_doc()
_PREFIX = _FLEET["instance"]
_SENTINEL = next(
    name for name, spec in _FLEET["integrations"].items() if "connectors" in spec
)
_INTEL = next(
    name for name, spec in _FLEET["integrations"].items() if "jobs" in spec
)
_CONN_KEY = next(iter(_FLEET["integrations"][_SENTINEL]["connectors"]))
_JOB_KEY = next(iter(_FLEET["integrations"][_INTEL]["jobs"]))
_CONN_PARENT = f"{_PREFIX}/integrations/{_SENTINEL}/connectors/{_CONN_KEY}"
_JOB_PARENT = f"{_PREFIX}/integrations/{_INTEL}/jobs/{_JOB_KEY}"


def _conn_name(env: str) -> str:
    inst = _FLEET["environments"][env]["deployments"]["alert-ingestion"][
        "connectorInstance"
    ]
    return f"{_CONN_PARENT}/connectorInstances/{inst}"


def _job_name(env: str) -> str:
    inst = _FLEET["environments"][env]["deployments"]["intel-sync"][
        "jobInstance"
    ]
    return f"{_JOB_PARENT}/jobInstances/{inst}"


def _integ_name(env: str) -> str:
    return f"{_PREFIX}/integrations/{_SENTINEL}/integrationInstances/{env}"


def _make_world(monkeypatch, seed: int):
    monkeypatch.setenv("PROJECT", PROJECT)
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    clock = SimClock(T0)
    sink = MemorySink()
    return World(fleet, seed, clock, sink)


def _error_shape(resp, code: int, status: str) -> None:
    assert resp.status_code == code
    body = resp.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message", "status"}
    assert body["error"]["code"] == code
    assert body["error"]["status"] == status
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


# -- item 1: fail closed ------------------------------------------------


def test_review1_dev_auth_off_is_401(monkeypatch):
    world = _make_world(monkeypatch, 201)
    client = TestClient(create_app(world, dev_auth=False))
    _error_shape(
        client.get(f"/v1/{_PREFIX}/environments"), 401, "UNAUTHENTICATED"
    )
    _error_shape(
        client.get(f"/v1/{_PREFIX}/environments", headers=_DEPLOYER),
        401,
        "UNAUTHENTICATED",
    )
    _error_shape(
        client.post(
            f"/v1/{_integ_name('alpha')}:executeTest",
            json={},
            headers=_DEPLOYER,
        ),
        401,
        "UNAUTHENTICATED",
    )


# -- item 2: permission sets and route map ------------------------------


def test_review2_permission_sets_match_mocks_table():
    assert READER_PERMISSIONS == frozenset(
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
    assert DEPLOYER_PERMISSIONS == READER_PERMISSIONS | frozenset(
        {
            "chronicle.connectorInstances.create",
            "chronicle.connectorInstances.update",
            "chronicle.jobInstances.update",
            "chronicle.jobInstances.run",
            "chronicle.environments.create",
        }
    )
    assert set(IDENTITY_PERMISSIONS) == {"obs-reader", "deployer"}
    assert (
        ROUTE_PERMISSIONS["connectorInstances.patch"]
        == "chronicle.connectorInstances.update"
    )
    assert (
        ROUTE_PERMISSIONS["connectorInstances.runOnDemand"]
        == "chronicle.connectorInstances.update"
    )
    assert (
        ROUTE_PERMISSIONS["connectorInstances.setLogsCollection"]
        == "chronicle.connectorInstances.update"
    )
    assert (
        ROUTE_PERMISSIONS["jobInstances.runOnDemand"]
        == "chronicle.jobInstances.run"
    )
    assert (
        ROUTE_PERMISSIONS["integrationInstances.executeTest"]
        == "chronicle.integrationInstances.get"
    )


def test_review2_reader_denials_name_permission(monkeypatch):
    world = _make_world(monkeypatch, 202)
    client = TestClient(create_app(world, dev_auth=True))
    conn = _conn_name("alpha")
    body = client.get(f"/v1/{conn}", headers=_DEPLOYER).json()
    denied = [
        (
            client.patch(
                f"/v1/{conn}?updateMask=enabled",
                json={
                    "enabled": False,
                    "integrationVersion": body["integrationVersion"],
                },
                headers=_READER,
            ),
            "chronicle.connectorInstances.update",
        ),
        (
            client.post(
                f"/v1/{conn}:runOnDemand",
                json={"connectorInstance": body},
                headers=_READER,
            ),
            "chronicle.connectorInstances.update",
        ),
        (
            client.post(
                f"/v1/{conn}:setLogsCollection",
                json={"enabled": True},
                headers=_READER,
            ),
            "chronicle.connectorInstances.update",
        ),
        (
            client.post(
                f"/v1/{_job_name('alpha')}:runOnDemand",
                json={},
                headers=_READER,
            ),
            "chronicle.jobInstances.run",
        ),
        (
            client.post(
                f"/v1/{_PREFIX}/environments",
                json={"displayName": "X"},
                headers=_READER,
            ),
            "chronicle.environments.create",
        ),
    ]
    for resp, permission in denied:
        assert resp.status_code == 403
        err = resp.json()["error"]
        assert err["status"] == "PERMISSION_DENIED"
        assert permission in err["message"]


def test_review2_reader_reads_and_deployer_writes(monkeypatch):
    world = _make_world(monkeypatch, 203)
    client = TestClient(create_app(world, dev_auth=True))
    conn = _conn_name("alpha")
    assert (
        client.get(f"/v1/{_PREFIX}/environments", headers=_READER).status_code
        == 200
    )
    assert client.get(f"/v1/{conn}", headers=_READER).status_code == 200
    assert (
        client.get(f"/v1/{conn}/logs", headers=_READER).status_code == 200
    )
    assert (
        client.post(
            f"/v1/{_integ_name('alpha')}:executeTest",
            json={},
            headers=_READER,
        ).status_code
        == 200
    )
    body = client.get(f"/v1/{conn}", headers=_DEPLOYER).json()
    patched = client.patch(
        f"/v1/{conn}?updateMask=enabled",
        json={"enabled": False, "integrationVersion": body["integrationVersion"]},
        headers=_DEPLOYER,
    )
    assert patched.status_code == 200


# -- item 3: chaos requires deployer ------------------------------------


def test_review3_chaos_requires_deployer(monkeypatch):
    world = _make_world(monkeypatch, 204)
    client = TestClient(create_app(world, chaos=True, dev_auth=True))
    fault = {"type": "connector_stall", "environment": "alpha"}
    _error_shape(client.post("/_chaos/faults", json=fault), 401, "UNAUTHENTICATED")
    denied = client.post("/_chaos/faults", json=fault, headers=_READER)
    assert denied.status_code == 403
    assert denied.json()["error"]["status"] == "PERMISSION_DENIED"
    assert client.get("/_chaos/faults", headers=_READER).status_code == 403
    assert (
        client.post("/_chaos/schedule", json=[], headers=_READER).status_code
        == 403
    )
    created = client.post("/_chaos/faults", json=fault, headers=_DEPLOYER)
    assert created.status_code == 200
    fid = created.json()["id"]
    assert client.get("/_chaos/faults", headers=_DEPLOYER).status_code == 200
    assert (
        client.delete(f"/_chaos/faults/{fid}", headers=_READER).status_code
        == 403
    )
    assert (
        client.delete(f"/_chaos/faults/{fid}", headers=_DEPLOYER).status_code
        == 200
    )


# -- item 4: idempotent clear -------------------------------------------


def test_review4_double_clear_is_noop(monkeypatch):
    world = _make_world(monkeypatch, 205)
    fid = world.inject(FaultSpec(type="connector_stall", environment="alpha"))
    world.clear(fid)
    world.clear(fid)
    assert world.list_faults() == []


def test_review4_unknown_clear_is_404(monkeypatch):
    world = _make_world(monkeypatch, 206)
    try:
        world.clear("fault-999")
    except ApiError as exc:
        assert exc.code == 404
        assert exc.status == "NOT_FOUND"
    else:
        raise AssertionError("expected ApiError")


def test_review4_toggle_then_clear_is_noop(monkeypatch):
    world = _make_world(monkeypatch, 207)
    fid = world.inject(FaultSpec(type="connector_stall", environment="alpha"))
    name = _conn_name("alpha")
    world.patch_connector(
        name, ["enabled"], {"enabled": False, "integrationVersion": "1.0"}
    )
    world.patch_connector(
        name, ["enabled"], {"enabled": True, "integrationVersion": "1.0"}
    )
    assert world.list_faults() == []
    world.clear(fid)
    fid4 = world.inject(
        FaultSpec(type="job_hollow_success", environment="alpha")
    )
    world.clear(fid4)
    world.clear(fid4)
    params = world.get_job(_job_name("alpha"))["parameters"]
    lookback = [p for p in params if p["displayName"] == "Lookback Hours"][0]
    assert lookback["value"] == "24"


# -- item 5: Google error shape everywhere ------------------------------


def test_review5_unmatched_route_is_404(monkeypatch):
    world = _make_world(monkeypatch, 208)
    client = TestClient(create_app(world, dev_auth=True))
    _error_shape(
        client.get("/v1/bogus", headers=_DEPLOYER), 404, "NOT_FOUND"
    )


def test_review5_bad_pagesize_is_400(monkeypatch):
    world = _make_world(monkeypatch, 209)
    client = TestClient(create_app(world, dev_auth=True))
    _error_shape(
        client.get(f"/v1/{_conn_name('alpha')}/logs?pageSize=abc",
                   headers=_DEPLOYER),
        400,
        "INVALID_ARGUMENT",
    )
    _error_shape(
        client.get(f"/v1/{_job_name('alpha')}/logs?pageSize=-1",
                   headers=_DEPLOYER),
        400,
        "INVALID_ARGUMENT",
    )


def test_review5_unknown_parent_list_is_404(monkeypatch):
    world = _make_world(monkeypatch, 210)
    client = TestClient(create_app(world, dev_auth=True))
    _error_shape(
        client.get(
            f"/v1/{_PREFIX}/integrations/Nope/connectors/nope/connectorInstances",
            headers=_DEPLOYER,
        ),
        404,
        "NOT_FOUND",
    )
    _error_shape(
        client.get(
            f"/v1/{_PREFIX}/integrations/{_INTEL}/jobs/nope/jobInstances",
            headers=_DEPLOYER,
        ),
        404,
        "NOT_FOUND",
    )
    _error_shape(
        client.get(
            f"/v1/{_PREFIX}/integrations/Nope/integrationInstances",
            headers=_DEPLOYER,
        ),
        404,
        "NOT_FOUND",
    )


def test_review5_bad_chaos_body_is_400(monkeypatch):
    world = _make_world(monkeypatch, 211)
    client = TestClient(create_app(world, chaos=True, dev_auth=True))
    _error_shape(
        client.post("/_chaos/faults", json={}, headers=_DEPLOYER),
        400,
        "INVALID_ARGUMENT",
    )


# -- item 6: updateMask validation --------------------------------------


def test_review6_connector_bad_mask_changes_nothing(monkeypatch):
    world = _make_world(monkeypatch, 212)
    client = TestClient(create_app(world, dev_auth=True))
    conn = _conn_name("alpha")
    before = client.get(f"/v1/{conn}", headers=_DEPLOYER).json()
    for mask in ("status", "enabled,status", "", "version"):
        resp = client.patch(
            f"/v1/{conn}?updateMask={mask}",
            json={"enabled": False, "integrationVersion": "1.0"},
            headers=_DEPLOYER,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    resp = client.patch(
        f"/v1/{conn}",
        json={"enabled": False, "integrationVersion": "1.0"},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 400
    after = client.get(f"/v1/{conn}", headers=_DEPLOYER).json()
    assert after == before


def test_review6_job_bad_mask_changes_nothing(monkeypatch):
    world = _make_world(monkeypatch, 213)
    client = TestClient(create_app(world, dev_auth=True))
    job = _job_name("alpha")
    before = client.get(f"/v1/{job}", headers=_DEPLOYER).json()
    resp = client.patch(
        f"/v1/{job}?updateMask=lastRunStatus",
        json={"lastRunStatus": "ERROR"},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    resp = client.patch(
        f"/v1/{job}?updateMask=",
        json={"enabled": False},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 400
    after = client.get(f"/v1/{job}", headers=_DEPLOYER).json()
    assert after == before
