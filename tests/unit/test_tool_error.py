"""D5 review round 2: ToolSet internal errors use rule tool.error."""

from datetime import datetime, timezone
from pathlib import Path

from fleetsre.clock import SimClock
from fleetsre.contracts import load_contract
from fleetsre.mock.fleet import load_fleet
from fleetsre.registry import Registry, deployments_from_fleet
from fleetsre.sre.session import Session
from fleetsre.sre.tools import ToolSet

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 29, 14, 33, 15, tzinfo=timezone.utc)


def test_internal_exception_reported_as_tool_error():
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / "alert-ingestion.json")
    registry = Registry(
        [contract], deployments_from_fleet(fleet, contract)
    )
    tools = ToolSet(
        None, None, None, registry, clock=SimClock(NOW),
        verification=True,
    )
    session = Session(incident_id="inc-x")
    out = tools.call("query_logs", {
        "deployment": "alert-ingestion/alpha",
        "window": {"lastMinutes": 10}, "filter": {},
        "rationale": "t",
    }, session)
    assert session.calls[out["callId"]].verdict == "refused"
    problems = out["result"].get("problems", [])
    assert any(p.startswith("tool.error:") for p in problems), out
    assert not any(p.startswith("tool.unknown:") for p in problems), out
