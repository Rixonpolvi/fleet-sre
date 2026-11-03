"""D8 web unit collateral: unknown incidents are 404, never 500.

Acceptance pins the happy paths; these pin the not-found paths: an
unknown incident ID is 404 on the incident page, the incident API and
the turn-input API, a well-formed but non-act incident ID is 404 on
turn input (the request log is per runtime), and a KeyError from the
manager is treated as 404 like a ValueError.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "acceptance" / "d8")
)

from d8_helpers import (  # noqa: E402
    ACTS_PATH,
    ADMIN_HEADERS,
    drive,
    make_client,
    make_host,
)

UNKNOWN = "inc-alert-ingestion-alpha-dead-connector-20200101T000000Z"


def test_unknown_incident_is_404_on_all_three_gets():
    """Unknown IDs are 404 on the page, the API and turn input."""
    host = make_host("F1")
    client = make_client(host)
    page = client.get(f"/incidents/{UNKNOWN}")
    assert page.status_code == 404
    api = client.get(f"/api/incidents/{UNKNOWN}")
    assert api.status_code == 404
    assert "error" in api.json()
    turn = client.get(f"/api/incidents/{UNKNOWN}/turns/1/input")
    assert turn.status_code == 404
    assert "error" in turn.json()


def test_turn_input_for_non_act_incident_is_404():
    """Turn input for another act's incident ID is 404 (fix 2)."""
    from fleetsre.control.incidents import IncidentState
    from fleetsre.demo.acts import load_acts

    other = load_acts(str(ACTS_PATH))["F2"].incident
    host = make_host("F1")
    drive(host.current, IncidentState.AWAITING_APPROVAL)
    client = make_client(host)
    assert other != host.current.act.incident
    response = client.get(f"/api/incidents/{other}/turns/1/input")
    assert response.status_code == 404


def test_key_error_from_manager_is_404(monkeypatch):
    """A KeyError from manager.get is 404 on GET and POST paths."""

    def _missing(_incident_id):
        raise KeyError(_incident_id)

    host = make_host("F1")
    rt = host.current
    monkeypatch.setattr(rt.manager, "get", _missing)
    client = make_client(host)
    assert client.get(f"/api/incidents/{rt.act.incident}").status_code == 404
    denied = client.post(
        f"/api/incidents/{rt.act.incident}/approve",
        json={"proposal_hash": "0" * 64},
        headers=ADMIN_HEADERS,
    )
    assert denied.status_code == 404
