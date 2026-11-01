"""D5 review round 3: render_incident names components and tripwires."""

from datetime import datetime, timezone
from pathlib import Path

from fleetsre.contracts import load_contract
from fleetsre.evaluator.engine import TripEvent
from fleetsre.mock.fleet import load_fleet
from fleetsre.registry import Registry, deployments_from_fleet
from fleetsre.sre.schemas import Incident, render_incident

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 29, 14, 33, 15, tzinfo=timezone.utc)


def _incident() -> tuple[Incident, Registry]:
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / "alert-ingestion.json")
    registry = Registry(
        [contract], deployments_from_fleet(fleet, contract)
    )
    event = TripEvent(
        kind="trip", deployment="alert-ingestion/alpha",
        tripwire="dead-connector", at=NOW, reason="r", evidence=("a",),
    )
    return Incident.from_trip(event, registry, NOW), registry


def test_render_states_component_and_lists():
    incident, registry = _incident()
    text = render_incident(incident, registry)
    assert (
        "Component: connector (the tripped tripwire's component)" in text
    )
    contract = registry.contract("alert-ingestion")
    for component in contract.components:
        assert component.id in text
        assert component.kind in text
    for name in incident.resources.values():
        assert name in text
    for tripwire in contract.tripwires:
        assert tripwire.id in text
        assert tripwire.component in text


def test_render_never_includes_descriptions():
    incident, registry = _incident()
    text = render_incident(incident, registry)
    contract = registry.contract("alert-ingestion")
    for tripwire in contract.tripwires:
        assert tripwire.description
        assert tripwire.description not in text
