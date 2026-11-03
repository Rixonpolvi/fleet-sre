"""D8 acceptance: incident view (d8-pins 13, Rows T2)."""

from d8_helpers import ADMIN_HEADERS, drive, make_client, make_host


def _incident(client, incident_id):
    response = client.get(f"/api/incidents/{incident_id}")
    assert response.status_code == 200
    return response.json()


def test_d8_t2_incident_view():
    """D8-T2: F3 hypotheses, resolved evidence, diff, verification, postmortem.

    At AWAITING_APPROVAL the view carries the session's hypotheses, every
    evidence item resolved per pin 13, category schema_drift, the -1 diff
    (P3: before "" after "-1") and null verification. After reject, approve
    and drive to RESOLVED, verification is verified with a resolved
    postmortem, and the HTML shows the category.
    """
    from fleetsre.control.incidents import IncidentState
    from fleetsre.ports import MemoryLogQuery

    host = make_host("F3")
    rt = host.current
    client = make_client(host)
    incident_id = rt.act.incident

    drive(rt, IncidentState.AWAITING_APPROVAL)
    view = _incident(client, incident_id)
    session = rt.store.load(incident_id)

    assert view["id"] == incident_id
    assert view["deployment"] == "alert-ingestion/alpha"
    assert view["state"] == "AWAITING_APPROVAL"
    assert [h["id"] for h in view["hypotheses"]] == list(session.hypotheses)
    for item in view["hypotheses"]:
        hypothesis = session.hypotheses[item["id"]]
        assert item["statement"] == hypothesis.statement
        assert item["wouldConfirm"] == hypothesis.wouldConfirm
        assert item["alternative"] == hypothesis.alternative

    log_query = MemoryLogQuery(rt.sink)
    assert len(view["evidence"]) == len(session.evidence)
    for item in view["evidence"]:
        recorded = session.evidence[item["id"]]
        assert item["hypothesis"] == recorded.hypothesis
        assert item["stance"] == recorded.stance
        assert item["why"] == recorded.why
        assert item["recordedAtTurn"] == recorded.recordedAtTurn
        source = item["source"]
        if recorded.insertId is not None:
            assert source["kind"] == "log"
            assert source["insertId"] == recorded.insertId
            entry = log_query.get(recorded.insertId)
            assert entry is not None
            assert (
                source["timestamp"].replace("Z", "+00:00")
                == entry.timestamp.isoformat()
            )
            assert source["severity"] == entry.severity
        else:
            assert source["kind"] == "toolResult"
            assert source["callId"] == recorded.callId
            call = session.calls[source["callId"]]
            assert source["tool"] == call.tool
            assert source["field"] == recorded.field
            assert source["value"] == recorded.value

    assert view["diagnosis"] is not None
    assert view["diagnosis"]["category"] == "schema_drift"
    proposal = view["proposal"]
    assert proposal is not None
    assert proposal["hash"] == rt.manager.get(incident_id).proposals[-1].hash
    assert proposal["diff"] == [{
        "field": "parameters[Unknown Severity Priority]",
        "before": "",
        "after": "-1",
    }]
    assert view["verification"] is None

    page = client.get(f"/incidents/{incident_id}")
    assert page.status_code == 200
    for item in view["hypotheses"]:
        assert item["statement"] in page.text
    assert "Unknown Severity Priority" in page.text
    assert "-1" in page.text

    rejection = client.post(
        f"/api/incidents/{incident_id}/reject",
        json={
            "proposal_hash": proposal["hash"],
            "notes": "unknown severities should map to 100",
        },
        headers=ADMIN_HEADERS,
    )
    assert rejection.status_code == 200
    assert rejection.json()["state"] == "INVESTIGATING"
    rt.advance(rt.act.tick_seconds)
    revised = _incident(client, incident_id)
    assert revised["state"] == "AWAITING_APPROVAL"
    approval = client.post(
        f"/api/incidents/{incident_id}/approve",
        json={"proposal_hash": revised["proposal"]["hash"]},
        headers=ADMIN_HEADERS,
    )
    assert approval.status_code == 200
    drive(rt, IncidentState.RESOLVED)

    resolved = _incident(client, incident_id)
    assert resolved["state"] == "RESOLVED"
    assert resolved["verification"] is not None
    assert resolved["verification"]["outcome"] == "verified"
    assert resolved["postmortem"] is not None
    assert resolved["postmortem"]["outcome"] == "resolved"
    page = client.get(f"/incidents/{incident_id}")
    assert page.status_code == 200
    assert resolved["postmortem"]["category"] in page.text
