"""D5: patch uses the stored parameter's type for the PASSWORD round-trip."""

import copy
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from fleetsre.clock import SimClock
from fleetsre.mock.chronicle_api import create_app
from fleetsre.mock.fleet import load_fleet
from fleetsre.mock.world import World
from fleetsre.ports import MemorySink

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)

_DEPLOYER = {"X-Fleetsre-Identity": "deployer"}


def _world(seed: int = 11) -> tuple[World, TestClient, str]:
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(fleet, seed, clock, sink)
    client = TestClient(create_app(world, dev_auth=True))
    name = (
        f"{fleet.instance}/integrations/MockSentinel/connectors/"
        "mock-sentinel-incidents/connectorInstances/alpha-sentinel"
    )
    return world, client, name


def _params(client: TestClient, name: str) -> list[dict]:
    resp = client.get(f"/v1/{name}", headers=_DEPLOYER)
    assert resp.status_code == 200
    return resp.json()["parameters"]


def test_password_mask_without_type_keeps_stored_secret():
    """A patch omitting type must not overwrite a secret with the mask."""
    world, client, name = _world()
    before = {p["displayName"]: p for p in world.get_connector(name)["parameters"]}
    assert before["Client Secret"]["type"] == "PASSWORD"
    assert before["Client Secret"]["value"] == "********"
    stored = next(
        p["value"] for p in world._connectors[name].params
        if p["displayName"] == "Client Secret"
    )
    assert stored not in ("", "********")
    patched = []
    for param in _params(client, name):
        item = {"displayName": param["displayName"], "value": param["value"]}
        patched.append(item)
    resp = client.patch(
        f"/v1/{name}?updateMask=parameters",
        json={"integrationVersion": "1.0", "parameters": patched},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    kept = next(
        p["value"] for p in world._connectors[name].params
        if p["displayName"] == "Client Secret"
    )
    assert kept == stored


def test_password_mask_with_type_keeps_stored_secret():
    """The typed round-trip still keeps the stored secret."""
    world, client, name = _world()
    stored = next(
        p["value"] for p in world._connectors[name].params
        if p["displayName"] == "Client Secret"
    )
    patched = copy.deepcopy(_params(client, name))
    resp = client.patch(
        f"/v1/{name}?updateMask=parameters",
        json={"integrationVersion": "1.0", "parameters": patched},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    kept = next(
        p["value"] for p in world._connectors[name].params
        if p["displayName"] == "Client Secret"
    )
    assert kept == stored


def test_new_password_value_overwrites():
    """A real value (not the mask) still overwrites the stored secret."""
    world, client, name = _world()
    patched = copy.deepcopy(_params(client, name))
    for param in patched:
        if param["displayName"] == "Client Secret":
            param["value"] = "rotated-secret-value"
    resp = client.patch(
        f"/v1/{name}?updateMask=parameters",
        json={"integrationVersion": "1.0", "parameters": patched},
        headers=_DEPLOYER,
    )
    assert resp.status_code == 200
    stored = next(
        p["value"] for p in world._connectors[name].params
        if p["displayName"] == "Client Secret"
    )
    assert stored == "rotated-secret-value"
