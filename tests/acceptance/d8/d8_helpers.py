"""D8 acceptance helpers: DemoHost without a realtime thread, driven by advance.

All tests are offline (d8-pins §Rows): DemoHost(load_acts()) with no thread,
driven by host.current.advance(...), and FastAPI's TestClient over
create_app(host, admin_token="t"). Admin requests send X-Fleetsre-Admin: t.
"""

import copy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
ACTS_PATH = ROOT / "fixtures" / "demo" / "acts.yaml"
ADMIN_TOKEN = "t"
ADMIN_HEADERS = {"X-Fleetsre-Admin": ADMIN_TOKEN}

READ_TOOLS = frozenset({
    "query_logs",
    "get_resource",
    "list_connector_logs",
    "list_job_logs",
    "test_integration_instance",
    "compare_siblings",
    "tripwire_history",
    "get_contract",
})


def make_host(act: str = "F1", **kwargs: Any) -> Any:
    """DemoHost over the committed acts, switched to `act` (started)."""
    from fleetsre.demo.acts import load_acts
    from fleetsre.demo.host import DemoHost

    host = DemoHost(load_acts(str(ACTS_PATH)), **kwargs)
    host.switch(act)
    return host


def make_client(host: Any, token: str = ADMIN_TOKEN) -> Any:
    """TestClient over create_app with the enrolment token."""
    from fastapi.testclient import TestClient

    from fleetsre.web.app import create_app

    return TestClient(create_app(host, admin_token=token))


def drive(rt: Any, until_state: Any, cap_s: int = 14400) -> Any:
    """Advance one tick at a time until the act's incident reaches the state."""
    tick = rt.act.tick_seconds
    steps = int(cap_s // tick) + 1
    for _ in range(steps):
        incidents = rt.manager.incidents()
        if incidents:
            record = next(
                (r for r in incidents if r.id == rt.act.incident),
                incidents[-1],
            )
            if record.state == until_state:
                return record
        rt.advance(tick)
    incidents = rt.manager.incidents()
    record = None
    if incidents:
        record = next(
            (r for r in incidents if r.id == rt.act.incident),
            incidents[-1],
        )
    state = record.state if record is not None else None
    raise AssertionError(
        f"act incident is {state}, want {until_state} within {cap_s} s"
    )


def approvals(rt: Any) -> Any:
    """The manager's ApprovalStore (IncidentManager keeps it private)."""
    return rt.manager._approvals


def transcripts(rt: Any, incident_id: str) -> list[dict[str, Any]]:
    """Every transcript record of the incident, across transcript IDs, in order."""
    record = rt.manager.get(incident_id)
    out: list[dict[str, Any]] = []
    for transcript_id in record.transcript_ids:
        out.extend(rt.store.transcript(transcript_id))
    return out


def elide_encrypted(obj: Any) -> Any:
    """Copy with every encrypted_content value replaced (d8-pins 14)."""
    obj = copy.deepcopy(obj)
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "encrypted_content" and isinstance(value, str):
                obj[key] = f"<encrypted: {len(value)} chars>"
            else:
                obj[key] = elide_encrypted(value)
        return obj
    if isinstance(obj, list):
        return [elide_encrypted(item) for item in obj]
    return obj

