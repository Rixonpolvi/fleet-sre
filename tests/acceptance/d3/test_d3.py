"""D3 acceptance tests: mock SecOps (local)."""

import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from fleetsre.adapters.jsonl import JsonlSink
from fleetsre.clock import SimClock
from fleetsre.logmodel import LogEntry, read_jsonl
from fleetsre.mock.chronicle_api import create_app
from fleetsre.mock.connector import map_priority
from fleetsre.mock.fleet import load_fleet
from fleetsre.mock.world import FaultSpec, World
from fleetsre.ports import MemorySink

ROOT = Path(__file__).resolve().parents[3]
PREFIX = "projects/fleet-sre-demo/locations/us/instances/mock-soar"
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)
PROJECT = "fleet-sre-test"

_DEPLOYER = {"X-Fleetsre-Identity": "deployer"}
_READER = {"X-Fleetsre-Identity": "obs-reader"}


def _fleet_doc() -> dict:
    return yaml.safe_load((ROOT / "fixtures" / "fleet.yaml").read_text())


# Integration names, connector/job path segments and instance IDs all come
# from fixtures/fleet.yaml (integrations keys, environments.*.deployments).
_FLEET = _fleet_doc()
_SENTINEL = next(
    name for name, spec in _FLEET["integrations"].items() if "connectors" in spec
)
_INTEL = next(
    name for name, spec in _FLEET["integrations"].items() if "jobs" in spec
)
_CONNECTOR_PARENT = (
    f"{PREFIX}/integrations/{_SENTINEL}/connectors/"
    f"{next(iter(_FLEET['integrations'][_SENTINEL]['connectors']))}"
)
_JOB_PARENT = (
    f"{PREFIX}/integrations/{_INTEL}/jobs/"
    f"{next(iter(_FLEET['integrations'][_INTEL]['jobs']))}"
)
_CONNECTOR_ENVS = [
    env
    for env, spec in _FLEET["environments"].items()
    if "alert-ingestion" in spec.get("deployments", {})
]
_JOB_ENVS = [
    env
    for env, spec in _FLEET["environments"].items()
    if "intel-sync" in spec.get("deployments", {})
]


def _spec(resource: str) -> dict:
    path = ROOT / "specs" / "chronicle" / f"{resource}.yaml"
    return yaml.safe_load(path.read_text())


def _fields(resource: str) -> list:
    return list(_spec(resource)["fields"])


def _make_world(monkeypatch, seed: int, start: datetime = T0):
    monkeypatch.setenv("PROJECT", PROJECT)
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    clock = SimClock(start)
    sink = MemorySink()
    world = World(fleet, seed, clock, sink)
    return world, clock, sink, fleet


def _client(world: World, chaos: bool = False) -> TestClient:
    app = create_app(world, chaos=chaos, dev_auth=True)
    return TestClient(app)


def _items(data: dict, *preferred: str) -> list:
    for key in preferred:
        if isinstance(data.get(key), list):
            return data[key]
    for value in data.values():
        if isinstance(value, list) and all(
            isinstance(v, dict) for v in value
        ):
            return value
    raise AssertionError(f"no list found in {sorted(data)}")


def _connector_name(instance_id: str) -> str:
    return f"{_CONNECTOR_PARENT}/connectorInstances/{instance_id}"


def _job_name(instance_id: str) -> str:
    return f"{_JOB_PARENT}/jobInstances/{instance_id}"


def _connector_instance(env: str) -> str:
    return _FLEET["environments"][env]["deployments"]["alert-ingestion"][
        "connectorInstance"
    ]


def _job_instance(env: str) -> str:
    return _FLEET["environments"][env]["deployments"]["intel-sync"][
        "jobInstance"
    ]


def _connector_name_for_env(env: str) -> str:
    return _connector_name(_connector_instance(env))


def _job_name_for_env(env: str) -> str:
    return _job_name(_job_instance(env))


def _integration_name(integration: str, instance: str) -> str:
    return f"{PREFIX}/integrations/{integration}/integrationInstances/{instance}"


def _completed(entries, env: str) -> list:
    return [
        e
        for e in entries
        if e.jsonPayload.get("event") == "connector.run.completed"
        and e.jsonPayload.get("environment") == env
    ]


def test_d3_t1_connector_shape(monkeypatch):
    """D3-T1: GET and list return exactly the doc field set for connectorInstances."""
    fields = _fields("connectorInstances")
    world, _clock, _sink, _fleet = _make_world(monkeypatch, seed=101)
    client = _client(world)
    resp = client.get(
        f"/v1/{_CONNECTOR_PARENT}/connectorInstances", headers=_DEPLOYER
    )
    assert resp.status_code == 200
    items = _items(resp.json(), "connectorInstances")
    assert len(items) == len(_CONNECTOR_ENVS)
    for item in items:
        assert set(item.keys()) == set(fields)
    for item in items:
        got = client.get(f"/v1/{item['name']}", headers=_DEPLOYER)
        assert got.status_code == 200
        assert set(got.json().keys()) == set(fields)


def test_d3_t2_resource_names(monkeypatch):
    """D3-T2: Every name uses the fixed prefix even with $PROJECT set."""
    world, _clock, _sink, _fleet = _make_world(monkeypatch, seed=102)
    client = _client(world)
    alpha = _connector_name_for_env("alpha")
    resp = client.post(
        f"/v1/{alpha}:setLogsCollection",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    world.step_until(T0 + timedelta(seconds=360))
    pats = {
        "env": re.compile(rf"^{re.escape(PREFIX)}/environments/[^/]+$"),
        "integration": re.compile(
            rf"^{re.escape(PREFIX)}/integrations/[^/]+"
            r"/integrationInstances/[^/]+$"
        ),
        "connector": re.compile(
            rf"^{re.escape(PREFIX)}/integrations/[^/]+/connectors/[^/]+"
            r"/connectorInstances/[^/]+$"
        ),
        "clog": re.compile(
            rf"^{re.escape(PREFIX)}/integrations/[^/]+/connectors/[^/]+"
            r"/connectorInstances/[^/]+/logs/[^/]+$"
        ),
        "job": re.compile(
            rf"^{re.escape(PREFIX)}/integrations/[^/]+/jobs/[^/]+"
            r"/jobInstances/[^/]+$"
        ),
        "jlog": re.compile(
            rf"^{re.escape(PREFIX)}/integrations/[^/]+/jobs/[^/]+"
            r"/jobInstances/[^/]+/logs/[^/]+$"
        ),
    }
    envs = _items(
        client.get(f"/v1/{PREFIX}/environments", headers=_DEPLOYER).json(),
        "environments",
    )
    assert len(envs) == 4
    for item in envs:
        assert pats["env"].match(item["name"])
    for integ in _FLEET["integrations"]:
        resp = client.get(
            f"/v1/{PREFIX}/integrations/{integ}/integrationInstances",
            headers=_DEPLOYER,
        )
        assert resp.status_code == 200
        for item in _items(resp.json(), "integrationInstances"):
            assert pats["integration"].match(item["name"])
    conns = _items(
        client.get(
            f"/v1/{_CONNECTOR_PARENT}/connectorInstances",
            headers=_DEPLOYER,
        ).json(),
        "connectorInstances",
    )
    for item in conns:
        assert pats["connector"].match(item["name"])
    jobs = _items(
        client.get(f"/v1/{_JOB_PARENT}/jobInstances", headers=_DEPLOYER).json(),
        "jobInstances",
    )
    for item in jobs:
        assert pats["job"].match(item["name"])
    clogs = _items(
        client.get(f"/v1/{alpha}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(clogs) > 0
    for rec in clogs:
        assert pats["clog"].match(rec["name"])
    jlogs = _items(
        client.get(
            f"/v1/{_job_name_for_env('alpha')}/logs", headers=_DEPLOYER
        ).json(),
        "logs",
    )
    assert len(jlogs) > 0
    for rec in jlogs:
        assert pats["jlog"].match(rec["name"])


def test_d3_t3_enums_redaction(monkeypatch):
    """D3-T3: Status and ParamType enums and PASSWORD redaction."""
    spec = _spec("connectorInstances")
    status_enum = spec["enums"]["status"]
    param_enum = spec["enums"]["paramType"]
    integ_param_enum = _spec("integrationInstances")["enums"]["paramType"]
    world, _clock, _sink, _fleet = _make_world(monkeypatch, seed=103)
    client = _client(world)
    resp = client.get(
        f"/v1/{_CONNECTOR_PARENT}/connectorInstances", headers=_DEPLOYER
    )
    items = _items(resp.json(), "connectorInstances")
    assert len(items) == len(_CONNECTOR_ENVS)
    for item in items:
        assert item["status"] in status_enum
        for param in item["parameters"]:
            assert param["type"] in param_enum
            if param["type"] == "PASSWORD":
                assert param["value"] == "********"
    for integ in _FLEET["integrations"]:
        resp = client.get(
            f"/v1/{PREFIX}/integrations/{integ}/integrationInstances",
            headers=_DEPLOYER,
        )
        assert resp.status_code == 200
        for item in _items(resp.json(), "integrationInstances"):
            for param in item["parameters"]:
                assert param["type"] in integ_param_enum
                if param["type"] == "PASSWORD":
                    assert param["value"] == "********"


def test_d3_t4_patch_semantics(monkeypatch):
    """D3-T4: PATCH changes only enabled, needs integrationVersion, bumps version."""
    world, _clock, _sink, _fleet = _make_world(monkeypatch, seed=104)
    client = _client(world)
    name = _connector_name_for_env("alpha")
    before = client.get(f"/v1/{name}", headers=_DEPLOYER).json()
    old_version = int(before["version"])
    old_enabled = before["enabled"]
    good = {"enabled": (not old_enabled), "integrationVersion": "1.0"}
    # integrationVersion value comes from the GET body.
    good["integrationVersion"] = before["integrationVersion"]
    resp = client.patch(
        f"/v1/{name}?updateMask=enabled", json=good, headers=_DEPLOYER
    )
    assert resp.status_code == 200
    after = resp.json()
    assert after["enabled"] == (not old_enabled)
    assert int(after["version"]) == old_version + 1
    for key in before:
        if key in ("enabled", "version", "status"):
            continue
        assert after[key] == before[key]
    assert after["status"] == "NOT_LIVE"
    bad = client.patch(
        f"/v1/{name}?updateMask=enabled",
        json={"enabled": old_enabled},
        headers=_DEPLOYER,
    )
    assert bad.status_code == 400
    err = bad.json()["error"]
    assert err["code"] == 400
    assert err["status"] == "INVALID_ARGUMENT"
    assert isinstance(err["message"], str) and err["message"]
    second = client.patch(
        f"/v1/{name}?updateMask=enabled",
        json={
            "enabled": old_enabled,
            "integrationVersion": before["integrationVersion"],
        },
        headers=_DEPLOYER,
    )
    assert second.status_code == 200
    assert int(second.json()["version"]) == old_version + 2
    assert second.json()["status"] == "LIVE"


def test_d3_t5_run_on_demand(monkeypatch):
    """D3-T5: runOnDemand adds one run and new ConnectorLogs, schedule kept."""
    # Request {"connectorInstance": ...} and response
    # {"debugOutput", "success", "sampleCases"} are from mocks.md §4.1:
    # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1beta/projects.locations.instances.integrations.connectors.connectorInstances/runOnDemand
    world, _clock, sink, _fleet = _make_world(monkeypatch, seed=105)
    client = _client(world)
    target = _connector_name_for_env("alpha")
    resp = client.post(
        f"/v1/{target}:setLogsCollection",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    world.step_until(T0 + timedelta(seconds=150))
    alpha_before = len(_completed(sink.entries, "alpha"))
    bravo_before = len(_completed(sink.entries, "bravo"))
    assert alpha_before == bravo_before
    logs_before = _items(
        client.get(f"/v1/{target}/logs", headers=_DEPLOYER).json(), "logs"
    )
    get_body = client.get(f"/v1/{target}", headers=_DEPLOYER).json()
    resp = client.post(
        f"/v1/{target}:runOnDemand",
        json={"connectorInstance": get_body},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    assert set(resp.json().keys()) == {
        "debugOutput",
        "success",
        "sampleCases",
    }
    assert len(_completed(sink.entries, "alpha")) == alpha_before + 1
    assert len(_completed(sink.entries, "bravo")) == bravo_before
    logs_mid = _items(
        client.get(f"/v1/{target}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(logs_mid) > len(logs_before)
    world.step_until(T0 + timedelta(seconds=300))
    alpha_all = sorted(
        _completed(sink.entries, "alpha"), key=lambda e: e.timestamp
    )
    bravo_all = sorted(
        _completed(sink.entries, "bravo"), key=lambda e: e.timestamp
    )
    assert len(alpha_all) == len(bravo_all) + 1
    twin_world, _twin_clock, twin_sink, _twin_fleet = _make_world(
        monkeypatch, seed=105
    )
    twin_client = _client(twin_world)
    twin_resp = twin_client.post(
        f"/v1/{target}:setLogsCollection",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert twin_resp.status_code == 200
    twin_world.step_until(T0 + timedelta(seconds=300))
    main_ts = Counter(e.timestamp for e in alpha_all)
    twin_ts = Counter(
        e.timestamp
        for e in _completed(twin_sink.entries, "alpha")
    )
    assert twin_ts - main_ts == Counter()
    assert sum((main_ts - twin_ts).values()) == 1


def test_d3_t6_set_logs_collection(monkeypatch):
    """D3-T6: Collection off by default, 3h period, clear and disable."""
    # Request {"enabled": bool} and response {"logEnableExpirationTime": ...}
    # are from mocks.md §4.1:
    # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.connectors.connectorInstances/setLogsCollection
    # The 3-hour period is from:
    # https://docs.cloud.google.com/chronicle/docs/soar/ingest/connectors/view-connector-logs
    world, clock, _sink, _fleet = _make_world(monkeypatch, seed=106)
    client = _client(world)
    alpha = _connector_name_for_env("alpha")
    bravo = _connector_name_for_env("bravo")
    world.step_until(T0 + timedelta(seconds=180))
    empty = _items(
        client.get(f"/v1/{alpha}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert empty == []
    enable_at = clock.now()
    resp = client.post(
        f"/v1/{alpha}:setLogsCollection",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    expected_ms = int(enable_at.timestamp() * 1000) + 3 * 3600 * 1000
    assert int(resp.json()["logEnableExpirationTime"]) == expected_ms
    got = client.get(f"/v1/{alpha}", headers=_DEPLOYER).json()
    assert str(got["loggingEnabledUntilUnixMs"]) == str(
        resp.json()["logEnableExpirationTime"]
    )
    world.step_until(T0 + timedelta(seconds=480))
    inside = _items(
        client.get(f"/v1/{alpha}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(inside) > 0
    assert any(rec["logLevel"] == "DEBUG" for rec in inside)
    names_before = {rec["name"] for rec in inside}
    enable2_at = clock.now()
    resp2 = client.post(
        f"/v1/{alpha}:setLogsCollection",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert resp2.status_code == 200
    cleared = _items(
        client.get(f"/v1/{alpha}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert cleared == []
    world.step_until(enable2_at + timedelta(seconds=120))
    after_second = _items(
        client.get(f"/v1/{alpha}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(after_second) > 0
    assert names_before.isdisjoint({rec["name"] for rec in after_second})
    end2 = enable2_at + timedelta(hours=3)
    world.step_until(end2 - timedelta(seconds=60))
    pre_end = _items(
        client.get(f"/v1/{alpha}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(pre_end) > 0
    expiry_ms = int(resp2.json()["logEnableExpirationTime"])
    for rec in pre_end:
        assert int(rec["time"]) < expiry_ms
    world.step_until(end2 + timedelta(seconds=1))
    at_end = _items(
        client.get(f"/v1/{alpha}/logs", headers=_DEPLOYER).json(), "logs"
    )
    world.step_until(end2 + timedelta(seconds=180))
    past_end = _items(
        client.get(f"/v1/{alpha}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(past_end) == len(at_end)
    for rec in past_end:
        assert int(rec["time"]) < expiry_ms
    bravo_on = client.post(
        f"/v1/{bravo}:setLogsCollection",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert bravo_on.status_code == 200
    world.step_until(clock.now() + timedelta(seconds=120))
    active = _items(
        client.get(f"/v1/{bravo}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(active) > 0
    off = client.post(
        f"/v1/{bravo}:setLogsCollection",
        json={"enabled": False},
        headers=_DEPLOYER,
    )
    assert off.status_code == 200
    world.step_until(clock.now() + timedelta(seconds=120))
    stopped = _items(
        client.get(f"/v1/{bravo}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(stopped) == len(active)


def test_d3_t7_connector_logs_list(monkeypatch):
    """D3-T7: ConnectorLog fields, newest first and pagination."""
    fields = _fields("connectorInstanceLogs")
    levels = _spec("connectorInstanceLogs")["enums"]["logLevel"]
    world, _clock, _sink, _fleet = _make_world(monkeypatch, seed=107)
    client = _client(world)
    target = _connector_name_for_env("alpha")
    resp = client.post(
        f"/v1/{target}:setLogsCollection",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    world.step_until(T0 + timedelta(seconds=600))
    full = _items(
        client.get(f"/v1/{target}/logs?pageSize=1000", headers=_DEPLOYER).json(),
        "logs",
    )
    assert len(full) >= 5
    for rec in full:
        assert set(rec.keys()) == set(fields)
        assert rec["logLevel"] in levels
    times = [int(rec["time"]) for rec in full]
    assert times == sorted(times, reverse=True)
    seen: list = []
    token = None
    while True:
        url = f"/v1/{target}/logs?pageSize=3"
        if token:
            url += f"&pageToken={token}"
        page_resp = client.get(url, headers=_DEPLOYER)
        assert page_resp.status_code == 200
        data = page_resp.json()
        page = _items(data, "logs")
        assert len(page) <= 3
        seen.extend(page)
        token = data.get("nextPageToken")
        if not token:
            break
    assert [rec["name"] for rec in seen] == [rec["name"] for rec in full]
    assert len({rec["name"] for rec in seen}) == len(seen)


def test_d3_t8_execute_test(monkeypatch):
    """D3-T8: executeTest successful flag and unchanged snapshot."""
    # Empty request and {"successful", "message"} response are from §4.1:
    # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.integrationInstances/executeTest
    world, _clock, _sink, _fleet = _make_world(monkeypatch, seed=108)
    client = _client(world)
    bravo = _integration_name(_SENTINEL, "bravo")
    alpha = _integration_name(_SENTINEL, "alpha")
    before = world.snapshot()
    resp = client.post(f"/v1/{bravo}:executeTest", json={}, headers=_DEPLOYER)
    assert resp.status_code == 200
    assert "successful" in resp.json()
    assert "success" not in resp.json()
    assert resp.json()["successful"] is True
    assert isinstance(resp.json()["message"], str)
    assert world.snapshot() == before
    world.inject(FaultSpec(type="credential_expiry", environment="bravo"))
    before_fault = world.snapshot()
    failed = client.post(
        f"/v1/{bravo}:executeTest", json={}, headers=_DEPLOYER
    )
    assert failed.status_code == 200
    assert failed.json()["successful"] is False
    assert isinstance(failed.json()["message"], str)
    assert failed.json()["message"]
    assert world.snapshot() == before_fault
    sibling = client.post(
        f"/v1/{alpha}:executeTest", json={}, headers=_DEPLOYER
    )
    assert sibling.json()["successful"] is True


def test_d3_t9_job_instances(monkeypatch):
    """D3-T9: Job shape, patch, runOnDemand and log status."""
    # runOnDemand request {"parameters": [...]} and response {"success": bool}
    # are from mocks.md §4.1:
    # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1beta/projects.locations.instances.integrations.jobs.jobInstances/runOnDemand
    fields = _fields("jobInstances")
    log_fields = _fields("jobInstanceLogs")
    log_status = _spec("jobInstanceLogs")["enums"]["status"]
    world, _clock, sink, _fleet = _make_world(monkeypatch, seed=109)
    client = _client(world)
    listed = _items(
        client.get(f"/v1/{_JOB_PARENT}/jobInstances", headers=_DEPLOYER).json(),
        "jobInstances",
    )
    assert len(listed) == len(_JOB_ENVS)
    for item in listed:
        assert set(item.keys()) == set(fields)
    name = _job_name_for_env("alpha")
    before = client.get(f"/v1/{name}", headers=_DEPLOYER).json()
    assert set(before.keys()) == set(fields)
    off = client.patch(
        f"/v1/{name}?updateMask=enabled",
        json={"enabled": False},
        headers=_DEPLOYER,
    )
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    on = client.patch(
        f"/v1/{name}?updateMask=enabled",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert on.status_code == 200
    bad = client.patch(
        f"/v1/{name}?updateMask=intervalSeconds",
        json={"intervalSeconds": 30},
        headers=_DEPLOYER,
    )
    assert bad.status_code == 400
    world.step_until(T0 + timedelta(seconds=300))

    def _jobs(env: str) -> list:
        return [
            e
            for e in sink.entries
            if e.jsonPayload.get("event") == "job.run.completed"
            and e.jsonPayload.get("environment") == env
        ]

    alpha_before = len(_jobs("alpha"))
    bravo_before = len(_jobs("bravo"))
    run = client.post(
        f"/v1/{name}:runOnDemand", json={}, headers=_DEPLOYER
    )
    assert run.status_code == 200
    assert run.json()["success"] is True
    assert len(_jobs("alpha")) == alpha_before + 1
    assert len(_jobs("bravo")) == bravo_before
    world.step_until(T0 + timedelta(seconds=600))
    logs = _items(
        client.get(f"/v1/{name}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(logs) > 0
    for rec in logs:
        assert set(rec.keys()) == set(log_fields)
        assert rec["status"] in log_status


def test_d3_t10_environments(monkeypatch):
    """D3-T10: Environments list has the four tenants with doc fields."""
    fields = _fields("environments")
    world, _clock, _sink, _fleet = _make_world(monkeypatch, seed=110)
    client = _client(world)
    resp = client.get(f"/v1/{PREFIX}/environments", headers=_DEPLOYER)
    assert resp.status_code == 200
    items = _items(resp.json(), "environments")
    assert len(items) == len(_FLEET["environments"])
    ids = set()
    for item in items:
        assert set(item.keys()) == set(fields)
        assert item["name"].startswith(PREFIX + "/environments/")
        ids.add(item["name"].rsplit("/", 1)[-1])
    assert ids == set(_FLEET["environments"])


def test_d3_t11_errors(monkeypatch):
    """D3-T11: Unknown resource returns 404 NOT_FOUND."""
    world, _clock, _sink, _fleet = _make_world(monkeypatch, seed=111)
    client = _client(world)
    resp = client.get(
        f"/v1/{PREFIX}/environments/no-such-env", headers=_DEPLOYER
    )
    assert resp.status_code == 404
    body = resp.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message", "status"}
    assert body["error"]["code"] == 404
    assert body["error"]["status"] == "NOT_FOUND"
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


def test_d3_t12_run_cadence(monkeypatch):
    """D3-T12: Hourly connector and job cadence within tolerance."""
    world, _clock, sink, _fleet = _make_world(monkeypatch, seed=112)
    world.step_until(T0 + timedelta(seconds=3600))
    for inst in [_connector_instance(env) for env in _CONNECTOR_ENVS]:
        count = sum(
            1
            for e in sink.entries
            if e.jsonPayload.get("event") == "connector.run.completed"
            and e.labels.get("connector_instance") == inst
        )
        assert 59 <= count <= 61
    for env in _JOB_ENVS:
        count = sum(
            1
            for e in sink.entries
            if e.jsonPayload.get("event") == "job.run.completed"
            and e.jsonPayload.get("environment") == env
        )
        assert 11 <= count <= 13


def test_d3_t13_alert_volume(monkeypatch):
    """D3-T13: Six-hour alert volume and uneven hourly spread."""
    world, _clock, sink, _fleet = _make_world(monkeypatch, seed=113)
    world.step_until(T0 + timedelta(seconds=6 * 3600))

    def _count(env: str) -> int:
        return sum(
            1
            for e in sink.entries
            if e.jsonPayload.get("event") == "alert.ingested"
            and e.jsonPayload.get("environment") == env
        )

    assert 504 <= _count("alpha") <= 936
    assert 126 <= _count("bravo") <= 234
    assert 2 <= _count("charlie") <= 30
    buckets = [0] * 6
    for entry in sink.entries:
        if (
            entry.jsonPayload.get("event") == "alert.ingested"
            and entry.jsonPayload.get("environment") == "alpha"
        ):
            hour = int((entry.timestamp - T0).total_seconds() // 3600)
            if 0 <= hour < 6:
                buckets[hour] += 1
    assert len(set(buckets)) > 1


def test_d3_t14_ingestion_records(monkeypatch):
    """D3-T14: Unique correlation ids and integer priorities."""
    world, _clock, sink, _fleet = _make_world(monkeypatch, seed=114)
    world.step_until(T0 + timedelta(seconds=3600))
    etl = [
        e
        for e in sink.entries
        if e.jsonPayload.get("event") == "alert.ingested"
    ]
    assert len(etl) > 0
    corr = [entry.labels["correlation_id"] for entry in etl]
    assert len(set(corr)) == len(corr)
    for entry in etl:
        priority = entry.jsonPayload["alert"]["priority"]
        assert isinstance(priority, int)
        assert priority in {-1, 40, 60, 80, 100}
    for env in _FLEET["environments"]:
        env_tickets = [
            entry.jsonPayload["alert"]["ticket_id"]
            for entry in etl
            if entry.jsonPayload.get("environment") == env
        ]
        assert len(set(env_tickets)) == len(env_tickets)
    for env in _FLEET["environments"]:
        fetched = sum(
            entry.jsonPayload["alertsFetched"]
            for entry in sink.entries
            if entry.jsonPayload.get("event") == "connector.run.completed"
            and entry.jsonPayload.get("environment") == env
        )
        ingested = sum(
            1 for entry in etl if entry.jsonPayload.get("environment") == env
        )
        assert fetched == ingested


def test_d3_t15_severity_map(monkeypatch):
    """D3-T15: map_priority units and F3 end to end with patch."""
    assert map_priority("High", "") == 80
    assert map_priority("Medium", "") == 60
    assert map_priority("Low", "") == 40
    assert map_priority("Informational", "") == -1
    assert map_priority("Critical", "") == "Critical"
    patched_value = map_priority("Critical", "100")
    assert patched_value == 100
    assert isinstance(patched_value, int)
    world, clock, sink, _fleet = _make_world(monkeypatch, seed=115)
    client = _client(world)
    world.inject(
        FaultSpec(
            type="severity_drift", environment="alpha", params={"rate": 0.5}
        )
    )
    world.step_until(T0 + timedelta(seconds=600))

    def _alpha_alerts():
        return [
            e
            for e in sink.entries
            if e.jsonPayload.get("event") == "alert.ingested"
            and e.jsonPayload.get("environment") == "alpha"
        ]

    before = _alpha_alerts()
    assert len(before) > 0
    assert any(
        isinstance(entry.jsonPayload["alert"]["priority"], str)
        for entry in before
    )
    name = _connector_name_for_env("alpha")
    body = client.get(f"/v1/{name}", headers=_DEPLOYER).json()
    params = [dict(p) for p in body["parameters"]]
    for param in params:
        if param["displayName"] == "Unknown Severity Priority":
            param["value"] = "100"
    patched = client.patch(
        f"/v1/{name}?updateMask=parameters",
        json={
            "parameters": params,
            "integrationVersion": body["integrationVersion"],
        },
        headers=_DEPLOYER,
    )
    assert patched.status_code == 200
    patch_at = clock.now()
    world.step_until(patch_at + timedelta(seconds=600))
    new = [e for e in _alpha_alerts() if e.timestamp > patch_at]
    assert len(new) > 0
    for entry in new:
        assert isinstance(entry.jsonPayload["alert"]["priority"], int)


def test_d3_t16_determinism(monkeypatch, tmp_path):
    """D3-T16: Same seed is byte-identical, different seed differs."""
    monkeypatch.setenv("PROJECT", PROJECT)
    fleet_path = ROOT / "fixtures" / "fleet.yaml"
    first = tmp_path / "first.jsonl"
    fleet = load_fleet(fleet_path)
    clock = SimClock(T0)
    sink = JsonlSink(str(first))
    world = World(fleet, 42, clock, sink)
    world.inject(
        FaultSpec(
            type="severity_drift", environment="alpha", params={"rate": 0.5}
        )
    )
    world.step_until(T0 + timedelta(seconds=7200))
    second = tmp_path / "second.jsonl"
    script = (
        "import sys;"
        "from datetime import datetime, timedelta, timezone;"
        "fleet_path, out_path, seed_s = sys.argv[1:4];"
        "from fleetsre.adapters.jsonl import JsonlSink;"
        "from fleetsre.clock import SimClock;"
        "from fleetsre.mock.fleet import load_fleet;"
        "from fleetsre.mock.world import FaultSpec, World;"
        "start = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc);"
        "fleet = load_fleet(fleet_path);"
        "clock = SimClock(start);"
        "sink = JsonlSink(out_path);"
        "world = World(fleet, int(seed_s), clock, sink);"
        "world.inject(FaultSpec(type='severity_drift',"
        " environment='alpha', params={'rate': 0.5}));"
        "world.step_until(start + timedelta(seconds=7200))"
    )
    proc = subprocess.run(
        [
            "env",
            f"PROJECT={PROJECT}",
            "PYTHONHASHSEED=4242",
            sys.executable,
            "-c",
            script,
            str(fleet_path),
            str(second),
            "42",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert first.read_bytes() == second.read_bytes()
    third = tmp_path / "third.jsonl"
    clock3 = SimClock(T0)
    sink3 = JsonlSink(str(third))
    world3 = World(load_fleet(fleet_path), 43, clock3, sink3)
    world3.inject(
        FaultSpec(
            type="severity_drift", environment="alpha", params={"rate": 0.5}
        )
    )
    world3.step_until(T0 + timedelta(seconds=7200))
    assert third.read_bytes() != first.read_bytes()


def test_d3_t17_f1_stall(monkeypatch):
    """D3-T17: Connector stall silences python and logs until restart."""
    world, clock, sink, _fleet = _make_world(monkeypatch, seed=117)
    client = _client(world)
    target = _connector_name_for_env("alpha")
    resp = client.post(
        f"/v1/{target}:setLogsCollection",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    world.step_until(T0 + timedelta(seconds=180))

    def _python(env: str):
        return [
            e
            for e in sink.entries
            if e.jsonPayload.get("environment") == env
            and e.resource.labels.get("container_name") == "python"
            and str(e.jsonPayload.get("event", "")).startswith("connector.")
        ]

    logs_before = _items(
        client.get(f"/v1/{target}/logs", headers=_DEPLOYER).json(), "logs"
    )
    world.inject(FaultSpec(type="connector_stall", environment="alpha"))
    fault_at = clock.now()
    world.step_until(fault_at + timedelta(seconds=300))
    stalled = [e for e in _python("alpha") if e.timestamp > fault_at]
    assert stalled == []
    logs_stalled = _items(
        client.get(f"/v1/{target}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(logs_stalled) == len(logs_before)
    status = client.get(f"/v1/{target}", headers=_DEPLOYER).json()
    assert status["status"] == "LIVE"
    sibling = [
        e
        for e in _completed(sink.entries, "bravo")
        if e.timestamp > fault_at
    ]
    assert 4 <= len(sibling) <= 6
    body = client.get(f"/v1/{target}", headers=_DEPLOYER).json()
    off = client.patch(
        f"/v1/{target}?updateMask=enabled",
        json={
            "enabled": False,
            "integrationVersion": body["integrationVersion"],
        },
        headers=_DEPLOYER,
    )
    assert off.status_code == 200
    on = client.patch(
        f"/v1/{target}?updateMask=enabled",
        json={
            "enabled": True,
            "integrationVersion": body["integrationVersion"],
        },
        headers=_DEPLOYER,
    )
    assert on.status_code == 200
    clear_at = clock.now()
    world.step_until(clear_at + timedelta(seconds=120))
    resumed = [e for e in _python("alpha") if e.timestamp > clear_at]
    assert len(resumed) >= 2
    logs_after = _items(
        client.get(f"/v1/{target}/logs", headers=_DEPLOYER).json(), "logs"
    )
    assert len(logs_after) > len(logs_stalled)


def test_d3_t18_f2_credential(monkeypatch):
    """D3-T18: Credential expiry warns every run and stops ingestion."""
    world, clock, sink, _fleet = _make_world(monkeypatch, seed=118)
    client = _client(world)
    target = _connector_name_for_env("bravo")
    resp = client.post(
        f"/v1/{target}:setLogsCollection",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    world.step_until(T0 + timedelta(seconds=120))
    world.inject(FaultSpec(type="credential_expiry", environment="bravo"))
    fault_at = clock.now()
    world.step_until(fault_at + timedelta(seconds=300))
    runs = [e for e in _completed(sink.entries, "bravo") if e.timestamp > fault_at]
    assert 4 <= len(runs) <= 6
    run_ids = {e.jsonPayload["runId"] for e in runs}
    warnings = [
        e
        for e in sink.entries
        if e.timestamp > fault_at
        and e.jsonPayload.get("environment") == "bravo"
        and e.jsonPayload.get("event") == "connector.run.warning"
    ]
    assert {e.jsonPayload["runId"] for e in warnings} == run_ids
    assert all(e.severity == "WARNING" for e in warnings)
    etl = [
        e
        for e in sink.entries
        if e.timestamp > fault_at
        and e.jsonPayload.get("event") == "alert.ingested"
        and e.jsonPayload.get("environment") == "bravo"
    ]
    assert etl == []
    status = client.get(f"/v1/{target}", headers=_DEPLOYER).json()
    assert status["status"] == "LIVE"
    logs = _items(
        client.get(f"/v1/{target}/logs", headers=_DEPLOYER).json(), "logs"
    )
    fault_ms = int(fault_at.timestamp() * 1000)
    warned = [
        rec
        for rec in logs
        if int(rec["time"]) > fault_ms and rec["logLevel"] == "WARNING"
    ]
    assert len(warned) >= len(runs)
    alpha_etl = [
        e
        for e in sink.entries
        if e.timestamp > fault_at
        and e.jsonPayload.get("event") == "alert.ingested"
        and e.jsonPayload.get("environment") == "alpha"
    ]
    assert len(alpha_etl) > 0
    alpha_warn = [
        e
        for e in sink.entries
        if e.timestamp > fault_at
        and e.jsonPayload.get("environment") == "alpha"
        and e.severity in ("WARNING", "ERROR")
    ]
    assert alpha_warn == []


def test_d3_t19_f3_drift(monkeypatch):
    """D3-T19: Severity drift keeps volume, patch restores int priorities."""
    world, clock, sink, _fleet = _make_world(monkeypatch, seed=119)
    client = _client(world)
    world.inject(
        FaultSpec(
            type="severity_drift", environment="alpha", params={"rate": 0.5}
        )
    )
    world.step_until(T0 + timedelta(seconds=3600))

    def _alpha():
        return [
            e
            for e in sink.entries
            if e.jsonPayload.get("event") == "alert.ingested"
            and e.jsonPayload.get("environment") == "alpha"
        ]

    drifted = _alpha()
    assert 84 <= len(drifted) <= 156
    assert any(
        isinstance(entry.jsonPayload["alert"]["priority"], str)
        for entry in drifted
    )
    name = _connector_name_for_env("alpha")
    body = client.get(f"/v1/{name}", headers=_DEPLOYER).json()
    params = [dict(p) for p in body["parameters"]]
    for param in params:
        if param["displayName"] == "Unknown Severity Priority":
            param["value"] = "100"
    resp = client.patch(
        f"/v1/{name}?updateMask=parameters",
        json={
            "parameters": params,
            "integrationVersion": body["integrationVersion"],
        },
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    patch_at = clock.now()
    world.step_until(patch_at + timedelta(seconds=600))
    new = [e for e in _alpha() if e.timestamp > patch_at]
    assert len(new) > 0
    for entry in new:
        assert isinstance(entry.jsonPayload["alert"]["priority"], int)


def test_d3_t20_f4_hollow(monkeypatch):
    """D3-T20: Hollow success zeroes syncs until Lookback Hours is fixed."""
    world, clock, sink, _fleet = _make_world(monkeypatch, seed=120)
    client = _client(world)
    world.inject(FaultSpec(type="job_hollow_success", environment="bravo"))
    name = _job_name_for_env("bravo")
    body = client.get(f"/v1/{name}", headers=_DEPLOYER).json()
    lookback = [
        p for p in body["parameters"] if p["displayName"] == "Lookback Hours"
    ][0]["value"]
    assert lookback == "0"
    world.step_until(T0 + timedelta(seconds=900))

    def _bravo_jobs():
        return [
            e
            for e in sink.entries
            if e.jsonPayload.get("event") == "job.run.completed"
            and e.jsonPayload.get("environment") == "bravo"
        ]

    hollow = _bravo_jobs()
    assert len(hollow) >= 2
    for entry in hollow:
        assert entry.jsonPayload["status"] == "SUCCESS"
        assert entry.jsonPayload["indicatorsSynced"] == 0
    patched_params = [dict(p) for p in body["parameters"]]
    for param in patched_params:
        if param["displayName"] == "Lookback Hours":
            param["value"] = "24"
    resp = client.patch(
        f"/v1/{name}?updateMask=parameters",
        json={"parameters": patched_params},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    patch_at = clock.now()
    world.step_until(patch_at + timedelta(seconds=600))
    new = [e for e in _bravo_jobs() if e.timestamp > patch_at]
    assert any(
        entry.jsonPayload["status"] == "SUCCESS"
        and entry.jsonPayload["indicatorsSynced"] > 0
        for entry in new
    )


def test_d3_t21_f5_error(monkeypatch):
    """D3-T21: Job error ends every run with ERROR still completed."""
    world, clock, sink, _fleet = _make_world(monkeypatch, seed=121)
    client = _client(world)
    world.inject(FaultSpec(type="job_error", environment="alpha"))
    fault_at = clock.now()
    world.step_until(T0 + timedelta(seconds=900))
    completed = [
        e
        for e in sink.entries
        if e.timestamp > fault_at
        and e.jsonPayload.get("event") == "job.run.completed"
        and e.jsonPayload.get("environment") == "alpha"
    ]
    assert len(completed) >= 2
    for entry in completed:
        assert entry.jsonPayload["status"] == "ERROR"
        assert entry.jsonPayload["indicatorsSynced"] == 0
        assert entry.severity == "ERROR"
    errors = [
        e
        for e in sink.entries
        if e.timestamp > fault_at
        and e.jsonPayload.get("event") == "job.run.error"
        and e.jsonPayload.get("environment") == "alpha"
    ]
    assert {e.jsonPayload["runId"] for e in errors} == {
        e.jsonPayload["runId"] for e in completed
    }
    status = client.get(
        f"/v1/{_job_name_for_env('alpha')}", headers=_DEPLOYER
    ).json()
    assert status["lastRunStatus"] == "ERROR"
    logs = _items(
        client.get(
            f"/v1/{_job_name_for_env('alpha')}/logs", headers=_DEPLOYER
        ).json(),
        "logs",
    )
    assert len(logs) > 0
    assert all(rec["status"] == "ERROR" for rec in logs[: len(completed)])


@pytest.mark.parametrize("rate", [0.2, 0.7])
def test_d3_t22_f6_flap(monkeypatch, rate):
    """D3-T22: Flap error fraction matches rate and still ingests."""
    world, _clock, sink, _fleet = _make_world(monkeypatch, seed=122)
    world.inject(
        FaultSpec(
            type="connector_flap",
            environment="alpha",
            params={"rate": rate},
        )
    )
    world.step_until(T0 + timedelta(seconds=100 * 60))
    completed = _completed(sink.entries, "alpha")
    assert 99 <= len(completed) <= 101
    run_ids = {e.jsonPayload["runId"] for e in completed}
    error_ids = {
        e.jsonPayload["runId"]
        for e in sink.entries
        if e.jsonPayload.get("event") == "connector.run.error"
        and e.jsonPayload.get("environment") == "alpha"
    }
    assert error_ids <= run_ids
    fraction = len(error_ids) / len(run_ids)
    assert (rate - 0.1) <= fraction <= (rate + 0.1)
    etl = sum(
        1
        for e in sink.entries
        if e.jsonPayload.get("event") == "alert.ingested"
        and e.jsonPayload.get("environment") == "alpha"
    )
    expected = 120 * (100 / 60)
    assert int(expected * 0.7) <= etl <= int(expected * 1.3)


def test_d3_t23_f7_stall(monkeypatch):
    """D3-T23: Job stall stops runs and goes stale until restart."""
    world, clock, sink, _fleet = _make_world(monkeypatch, seed=123)
    client = _client(world)
    name = _job_name_for_env("alpha")
    world.step_until(T0 + timedelta(seconds=300))
    scheduled_before = client.get(f"/v1/{name}", headers=_DEPLOYER).json()[
        "nextScheduledRunTime"
    ]
    world.inject(FaultSpec(type="job_stall", environment="alpha"))
    fault_at = clock.now()
    world.step_until(fault_at + timedelta(seconds=900))
    stalled = [
        e
        for e in sink.entries
        if e.timestamp > fault_at
        and e.jsonPayload.get("event") == "job.run.completed"
        and e.jsonPayload.get("environment") == "alpha"
    ]
    assert stalled == []
    scheduled = client.get(f"/v1/{name}", headers=_DEPLOYER).json()[
        "nextScheduledRunTime"
    ]
    assert scheduled == scheduled_before
    stale_at = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
    assert stale_at < clock.now()
    body = client.get(f"/v1/{name}", headers=_DEPLOYER).json()
    off = client.patch(
        f"/v1/{name}?updateMask=enabled",
        json={"enabled": False},
        headers=_DEPLOYER,
    )
    assert off.status_code == 200
    on = client.patch(
        f"/v1/{name}?updateMask=enabled",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    assert on.status_code == 200
    assert body["enabled"] is True
    clear_at = clock.now()
    world.step_until(clear_at + timedelta(seconds=600))
    resumed = [
        e
        for e in sink.entries
        if e.timestamp > clear_at
        and e.jsonPayload.get("event") == "job.run.completed"
        and e.jsonPayload.get("environment") == "alpha"
    ]
    assert len(resumed) >= 1


@pytest.mark.parametrize(
    "fault_type,params,is_job",
    [
        ("connector_stall", {}, False),
        ("credential_expiry", {}, False),
        ("severity_drift", {"rate": 0.5}, False),
        ("job_hollow_success", {}, True),
        ("job_error", {}, True),
        ("connector_flap", {"rate": 0.7}, False),
        ("job_stall", {}, True),
    ],
)
def test_d3_t24_clearing(monkeypatch, fault_type, params, is_job):
    """D3-T24: clear() restores normal behaviour within 2 intervals."""
    world, clock, sink, _fleet = _make_world(monkeypatch, seed=124)
    client = _client(world)
    fid = world.inject(
        FaultSpec(type=fault_type, environment="alpha", params=dict(params))
    )
    if not is_job:
        world.step_until(T0 + timedelta(seconds=300))
    else:
        world.step_until(T0 + timedelta(seconds=600))
    world.clear(fid)
    clear_at = clock.now()
    limit = clear_at + timedelta(seconds=600 if is_job else 120)
    if fault_type == "job_hollow_success":
        body = client.get(
            f"/v1/{_job_name_for_env('alpha')}", headers=_DEPLOYER
        ).json()
        lookback = [
            p
            for p in body["parameters"]
            if p["displayName"] == "Lookback Hours"
        ][0]["value"]
        assert lookback == "24"
    if not is_job:
        world.step_until(clear_at + timedelta(seconds=300))
    else:
        world.step_until(clear_at + timedelta(seconds=600))
    if fault_type == "connector_stall":
        resumed = [
            e
            for e in _completed(sink.entries, "alpha")
            if e.timestamp > clear_at
        ]
        assert len(resumed) >= 2
        assert min(e.timestamp for e in resumed) <= limit
    elif fault_type == "credential_expiry":
        warns = [
            e
            for e in sink.entries
            if e.timestamp > clear_at
            and e.jsonPayload.get("environment") == "alpha"
            and e.severity in ("WARNING", "ERROR")
        ]
        assert warns == []
        etl = [
            e
            for e in sink.entries
            if e.timestamp > clear_at
            and e.jsonPayload.get("event") == "alert.ingested"
            and e.jsonPayload.get("environment") == "alpha"
        ]
        assert len(etl) > 0
        assert min(e.timestamp for e in etl) <= limit
    elif fault_type == "severity_drift":
        cutoff = clear_at + timedelta(seconds=120)
        new = [
            e
            for e in sink.entries
            if e.timestamp > cutoff
            and e.jsonPayload.get("event") == "alert.ingested"
            and e.jsonPayload.get("environment") == "alpha"
        ]
        assert len(new) > 0
        for entry in new:
            assert isinstance(entry.jsonPayload["alert"]["priority"], int)
        ints_after_clear = [
            e
            for e in sink.entries
            if e.timestamp > clear_at
            and e.jsonPayload.get("event") == "alert.ingested"
            and e.jsonPayload.get("environment") == "alpha"
            and isinstance(e.jsonPayload["alert"]["priority"], int)
        ]
        assert len(ints_after_clear) > 0
        assert min(e.timestamp for e in ints_after_clear) <= limit
    elif fault_type == "job_hollow_success":
        new = [
            e
            for e in sink.entries
            if e.timestamp > clear_at
            and e.jsonPayload.get("event") == "job.run.completed"
            and e.jsonPayload.get("environment") == "alpha"
        ]
        good = [
            entry
            for entry in new
            if entry.jsonPayload["status"] == "SUCCESS"
            and entry.jsonPayload["indicatorsSynced"] > 0
        ]
        assert len(good) > 0
        assert min(e.timestamp for e in good) <= limit
    elif fault_type == "job_error":
        new = [
            e
            for e in sink.entries
            if e.timestamp > clear_at
            and e.jsonPayload.get("event") == "job.run.completed"
            and e.jsonPayload.get("environment") == "alpha"
        ]
        assert len(new) >= 1
        assert all(
            entry.jsonPayload["status"] == "SUCCESS" for entry in new
        )
        assert min(e.timestamp for e in new) <= limit
    elif fault_type == "connector_flap":
        errs = [
            e
            for e in sink.entries
            if e.timestamp > clear_at
            and e.jsonPayload.get("event") == "connector.run.error"
            and e.jsonPayload.get("environment") == "alpha"
        ]
        assert errs == []
        resumed = [
            e
            for e in _completed(sink.entries, "alpha")
            if e.timestamp > clear_at
        ]
        assert len(resumed) >= 2
        err_ids = {e.jsonPayload["runId"] for e in errs}
        normal = [
            e for e in resumed if e.jsonPayload["runId"] not in err_ids
        ]
        assert len(normal) > 0
        assert min(e.timestamp for e in normal) <= limit
    elif fault_type == "job_stall":
        resumed = [
            e
            for e in sink.entries
            if e.timestamp > clear_at
            and e.jsonPayload.get("event") == "job.run.completed"
            and e.jsonPayload.get("environment") == "alpha"
        ]
        assert len(resumed) >= 1
        assert min(e.timestamp for e in resumed) <= limit


def test_d3_t25_canary(monkeypatch):
    """D3-T25: Canary appears quickly, never under credential fault."""
    world, clock, sink, _fleet = _make_world(monkeypatch, seed=125)
    first_id = world.plant_canary("alpha")
    plant_at = clock.now()
    world.step_until(plant_at + timedelta(seconds=120))
    seen = [
        e
        for e in sink.entries
        if e.jsonPayload.get("event") == "alert.ingested"
        and e.jsonPayload.get("alert", {}).get("canary") is True
        and e.jsonPayload.get("alert", {}).get("ticket_id") == first_id
    ]
    assert len(seen) == 1
    assert seen[0].timestamp <= plant_at + timedelta(seconds=120)
    world.inject(FaultSpec(type="credential_expiry", environment="alpha"))
    second_id = world.plant_canary("alpha")
    fault_at = clock.now()
    world.step_until(fault_at + timedelta(seconds=600))
    missing = [
        e
        for e in sink.entries
        if e.jsonPayload.get("event") == "alert.ingested"
        and e.jsonPayload.get("alert", {}).get("ticket_id") == second_id
    ]
    assert missing == []


def test_d3_t26_chaos_gating(monkeypatch):
    """D3-T26: Chaos routes are 404 unless chaos is enabled."""
    world, _clock, _sink, _fleet = _make_world(monkeypatch, seed=126)
    off = _client(world, chaos=False)
    assert (
        off.post(
            "/_chaos/faults",
            json={"type": "connector_stall", "environment": "alpha"},
            headers=_DEPLOYER,
        ).status_code
        == 404
    )
    assert off.get("/_chaos/faults", headers=_DEPLOYER).status_code == 404
    assert (
        off.delete("/_chaos/faults/abc", headers=_DEPLOYER).status_code == 404
    )
    assert (
        off.post(
            "/_chaos/schedule", json=[], headers=_DEPLOYER
        ).status_code
        == 404
    )
    on = _client(world, chaos=True)
    created = on.post(
        "/_chaos/faults",
        json={
            "type": "severity_drift",
            "environment": "alpha",
            "params": {"rate": 0.5},
        },
        headers=_DEPLOYER,
    )
    assert created.status_code == 200
    fault_id = created.json()["id"]
    assert isinstance(fault_id, str) and fault_id
    listed = on.get("/_chaos/faults", headers=_DEPLOYER)
    assert listed.status_code == 200
    assert fault_id in json.dumps(listed.json())
    scheduled = on.post(
        "/_chaos/schedule",
        json=[{"at": 60, "type": "connector_stall", "environment": "alpha"}],
        headers=_DEPLOYER,
    )
    assert scheduled.status_code == 200
    cleared = on.delete(f"/_chaos/faults/{fault_id}", headers=_DEPLOYER)
    assert cleared.status_code in (200, 204)


def test_d3_t27_authorization(monkeypatch):
    """D3-T27: Reader is denied writes, deployer succeeds, anon is 401."""
    # Method bodies from mocks.md §4.1 (not in specs/):
    # runOnDemand: https://docs.cloud.google.com/chronicle/docs/reference/rest/v1beta/projects.locations.instances.integrations.connectors.connectorInstances/runOnDemand
    # setLogsCollection: https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.connectors.connectorInstances/setLogsCollection
    # job runOnDemand: https://docs.cloud.google.com/chronicle/docs/reference/rest/v1beta/projects.locations.instances.integrations.jobs.jobInstances/runOnDemand
    # executeTest: https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.integrationInstances/executeTest
    world, _clock, _sink, _fleet = _make_world(monkeypatch, seed=127)
    client = _client(world)
    conn = _connector_name_for_env("alpha")
    job = _job_name_for_env("alpha")
    conn_body = client.get(f"/v1/{conn}", headers=_DEPLOYER).json()
    env_create = {"displayName": "Auth Test", "description": "Auth Test env"}
    conn_create = {
        key: value
        for key, value in conn_body.items()
        if key
        not in (
            "name",
            "connectorDefinitionName",
            "statistics",
            "connectorId",
            "id",
            "identifier",
            "updateAvailable",
            "custom",
            "remote",
            "status",
            "version",
            "loggingEnabledUntilUnixMs",
        )
    }
    conn_create["displayName"] = "Auth Test"

    def _denied(resp):
        assert resp.status_code == 403
        err = resp.json()["error"]
        assert err["code"] == 403
        assert err["status"] == "PERMISSION_DENIED"

    _denied(
        client.post(
            f"/v1/{PREFIX}/environments",
            json=env_create,
            headers=_READER,
        )
    )
    created_env = client.post(
        f"/v1/{PREFIX}/environments",
        json=env_create,
        headers=_DEPLOYER,
    )
    assert 200 <= created_env.status_code < 300
    _denied(
        client.post(
            f"/v1/{_CONNECTOR_PARENT}/connectorInstances",
            json=conn_create,
            headers=_READER,
        )
    )
    created_conn = client.post(
        f"/v1/{_CONNECTOR_PARENT}/connectorInstances",
        json=conn_create,
        headers=_DEPLOYER,
    )
    assert 200 <= created_conn.status_code < 300
    _denied(
        client.patch(
            f"/v1/{conn}?updateMask=enabled",
            json={
                "enabled": False,
                "integrationVersion": conn_body["integrationVersion"],
            },
            headers=_READER,
        )
    )
    patched = client.patch(
        f"/v1/{conn}?updateMask=enabled",
        json={
            "enabled": False,
            "integrationVersion": conn_body["integrationVersion"],
        },
        headers=_DEPLOYER,
    )
    assert patched.status_code == 200
    client.patch(
        f"/v1/{conn}?updateMask=enabled",
        json={
            "enabled": True,
            "integrationVersion": conn_body["integrationVersion"],
        },
        headers=_DEPLOYER,
    )
    _denied(
        client.post(
            f"/v1/{conn}:runOnDemand",
            json={"connectorInstance": conn_body},
            headers=_READER,
        )
    )
    assert (
        client.post(
            f"/v1/{conn}:runOnDemand",
            json={"connectorInstance": conn_body},
            headers=_DEPLOYER,
        ).status_code
        == 200
    )
    _denied(
        client.post(
            f"/v1/{conn}:setLogsCollection",
            json={"enabled": True},
            headers=_READER,
        )
    )
    assert (
        client.post(
            f"/v1/{conn}:setLogsCollection",
            json={"enabled": True},
            headers=_DEPLOYER,
        ).status_code
        == 200
    )
    _denied(
        client.patch(
            f"/v1/{job}?updateMask=enabled",
            json={"enabled": False},
            headers=_READER,
        )
    )
    assert (
        client.patch(
            f"/v1/{job}?updateMask=enabled",
            json={"enabled": False},
            headers=_DEPLOYER,
        ).status_code
        == 200
    )
    client.patch(
        f"/v1/{job}?updateMask=enabled",
        json={"enabled": True},
        headers=_DEPLOYER,
    )
    _denied(
        client.post(f"/v1/{job}:runOnDemand", json={}, headers=_READER)
    )
    assert (
        client.post(
            f"/v1/{job}:runOnDemand", json={}, headers=_DEPLOYER
        ).status_code
        == 200
    )
    anon = client.get(f"/v1/{PREFIX}/environments")
    assert anon.status_code == 401
    integ = _integration_name(_SENTINEL, "alpha")
    assert (
        client.post(
            f"/v1/{integ}:executeTest", json={}, headers=_READER
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/{integ}:executeTest", json={}, headers=_DEPLOYER
        ).status_code
        == 200
    )


def test_d3_t28_log_validity(monkeypatch):
    """D3-T28: Two-hour fault cycle keeps every entry valid and labelled."""
    world, clock, sink, _fleet = _make_world(monkeypatch, seed=128)
    schedule = [
        FaultSpec(type="connector_stall", environment="alpha"),
        FaultSpec(type="credential_expiry", environment="bravo"),
        FaultSpec(
            type="severity_drift", environment="alpha", params={"rate": 0.5}
        ),
        FaultSpec(type="job_hollow_success", environment="bravo"),
        FaultSpec(type="job_error", environment="alpha"),
        FaultSpec(
            type="connector_flap", environment="alpha", params={"rate": 0.2}
        ),
        FaultSpec(type="job_stall", environment="bravo"),
    ]
    step = timedelta(seconds=7200 // (len(schedule) * 2))
    for fault in schedule:
        fid = world.inject(fault)
        world.step_until(clock.now() + step)
        world.clear(fid)
        world.step_until(clock.now() + step)
    assert len(sink.entries) > 0
    connector_labels = (
        "integration_name",
        "integration_version",
        "connector_name",
        "connector_instance",
    )
    job_labels = ("integration_name", "integration_version", "job_name")
    ids = [entry.insertId for entry in sink.entries]
    assert len(set(ids)) == len(ids)
    for entry in sink.entries:
        assert isinstance(entry, LogEntry)
        LogEntry.model_validate(entry.model_dump())
        assert entry.resource.type == "k8s_container"
        assert entry.resource.labels["namespace_name"] == "chronicle-soar"
        assert entry.resource.labels["container_name"] in {"python", "etl"}
        assert entry.resource.labels["project_id"] == PROJECT
        assert entry.logName == f"projects/{PROJECT}/logs/chronicle-soar"
        event = entry.jsonPayload.get("event", "")
        assert isinstance(event, str) and event
        if event.startswith("connector."):
            for label in connector_labels:
                assert label in entry.labels
        elif event.startswith("job."):
            for label in job_labels:
                assert label in entry.labels
        elif event.startswith("alert."):
            assert "correlation_id" in entry.labels
        else:
            raise AssertionError(f"unexpected event {event!r}")


def test_d3_t29_jsonl_sink(monkeypatch, tmp_path):
    """D3-T29: JsonlSink appends one entry per line re-readable as LogEntry."""
    monkeypatch.setenv("PROJECT", PROJECT)
    path = tmp_path / "sink.jsonl"
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    clock = SimClock(T0)
    sink = JsonlSink(str(path))
    world = World(fleet, 129, clock, sink)
    world.step_until(T0 + timedelta(seconds=600))
    first = read_jsonl(path)
    assert len(first) > 0
    lines = [
        line for line in path.read_text().splitlines() if line.strip()
    ]
    assert len(lines) == len(first)
    for line, entry in zip(lines, first):
        assert isinstance(entry, LogEntry)
        assert json.loads(line)["insertId"] == entry.insertId
    world.step_until(T0 + timedelta(seconds=900))
    second = read_jsonl(path)
    assert len(second) > len(first)
    assert [e.insertId for e in second[: len(first)]] == [
        e.insertId for e in first
    ]
