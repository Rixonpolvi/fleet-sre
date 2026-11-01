"""D5 review: incident ID round-trip for hyphenated names.

Incident.reconstruct matches the registry's services, environments and
tripwire IDs; it never splits the ID on "-" (service and tripwire names
contain hyphens).
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from fleetsre.contracts import load_contract
from fleetsre.evaluator.engine import TripEvent
from fleetsre.mock.fleet import load_fleet
from fleetsre.registry import Registry, deployments_from_fleet
from fleetsre.sre.schemas import Incident

ROOT = Path(__file__).resolve().parents[2]
AT = datetime(2026, 10, 29, 14, 33, 15, tzinfo=timezone.utc)


def _registry() -> Registry:
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contracts = [
        load_contract(ROOT / "contracts" / "alert-ingestion.json"),
        load_contract(ROOT / "contracts" / "intel-sync.json"),
    ]
    deployments = []
    for contract in contracts:
        deployments.extend(deployments_from_fleet(fleet, contract))
    return Registry(contracts, deployments)


@pytest.mark.parametrize(
    ("service", "env", "tripwire"),
    [
        ("intel-sync", "alpha", "dead-job"),
        ("alert-ingestion", "bravo", "connector-errors"),
    ],
)
def test_reconstruct_round_trip(service: str, env: str, tripwire: str):
    registry = _registry()
    deployment = f"{service}/{env}"
    event = TripEvent(
        kind="trip", deployment=deployment, tripwire=tripwire,
        at=AT, reason="probe", evidence=("a", "b"),
    )
    incident = Incident.from_trip(event, registry, AT)
    assert incident.id == (
        f"inc-{service}-{env}-{tripwire}-20261029T143315Z"
    )
    back = Incident.reconstruct(incident.id, registry, AT)
    assert back.id == incident.id
    assert back.deployment == deployment
    assert back.service == service
    assert back.tripwire == tripwire
    assert back.component == incident.component
    assert back.resources == incident.resources
    assert back.siblings == incident.siblings
    assert back.trip_event.at == AT


@pytest.mark.parametrize(
    "bad_id",
    [
        "inc-t1",
        "inc-alert-ingestion-alpha-dead-connector-notastamp",
        "inc-unknown-alpha-dead-connector-20261029T143315Z",
        "inc-alert-ingestion-bravo-nope-20261029T143315Z",
        "inc-alert-ingestion-nova-dead-connector-20261029T143315Z",
    ],
)
def test_reconstruct_rejects_bad_ids(bad_id: str):
    with pytest.raises(ValueError):
        Incident.reconstruct(bad_id, _registry(), AT)
