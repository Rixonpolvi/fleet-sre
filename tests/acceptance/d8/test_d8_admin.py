"""D8 acceptance: approve, reject and the admin credential (d8-pins 10-11, T3)."""

from d8_helpers import (
    ADMIN_HEADERS,
    ADMIN_TOKEN,
    approvals,
    drive,
    make_client,
    make_host,
)


def _latest_hash(rt, incident_id):
    return rt.manager.get(incident_id).proposals[-1].hash


def test_d8_t3_approve_reject():
    """D8-T3: approve/reject move the incident; strangers get 403, cookie works.

    F1 at AWAITING_APPROVAL: a wrong hash is 409, the latest hash approves
    to APPLYING with the approval recorded for operator. Fresh F3: empty
    notes are 409, notes reject to INVESTIGATING with a reopen pending and
    one Rejection. Without the credential every pin-10 POST is 403 and
    nothing changes. GET /?admin=t sets the cookie, which authorises a POST.
    """
    from fleetsre.control.incidents import IncidentState

    host = make_host("F1")
    rt = host.current
    client = make_client(host)
    incident_id = rt.act.incident
    drive(rt, IncidentState.AWAITING_APPROVAL)
    good_hash = _latest_hash(rt, incident_id)

    wrong = client.post(
        f"/api/incidents/{incident_id}/approve",
        json={"proposal_hash": "0" * 64},
        headers=ADMIN_HEADERS,
    )
    assert wrong.status_code == 409
    assert "error" in wrong.json()
    assert approvals(rt).records() == []
    assert rt.manager.get(incident_id).state == IncidentState.AWAITING_APPROVAL

    approved = client.post(
        f"/api/incidents/{incident_id}/approve",
        json={"proposal_hash": good_hash},
        headers=ADMIN_HEADERS,
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "APPLYING"
    assert rt.manager.get(incident_id).state == IncidentState.APPLYING
    approval = approvals(rt).approval_for(good_hash)
    assert approval is not None
    assert approval.approver == "operator"

    host3 = make_host("F3")
    rt3 = host3.current
    client3 = make_client(host3)
    incident3 = rt3.act.incident
    drive(rt3, IncidentState.AWAITING_APPROVAL)
    hash3 = _latest_hash(rt3, incident3)

    empty = client3.post(
        f"/api/incidents/{incident3}/reject",
        json={"proposal_hash": hash3, "notes": ""},
        headers=ADMIN_HEADERS,
    )
    assert empty.status_code == 409
    assert approvals(rt3).records() == []

    notes = "unknown severities should map to 100"
    rejected = client3.post(
        f"/api/incidents/{incident3}/reject",
        json={"proposal_hash": hash3, "notes": notes},
        headers=ADMIN_HEADERS,
    )
    assert rejected.status_code == 200
    record3 = rt3.manager.get(incident3)
    assert record3.state == IncidentState.INVESTIGATING
    assert record3.pending is not None
    assert record3.pending.kind == "reopen"
    stored = approvals(rt3).records()
    assert len(stored) == 1
    assert stored[0].notes == notes

    # No credential: every pin-10 POST is 403 and nothing changes.
    host_f = make_host("F1")
    rtf = host_f.current
    client_f = make_client(host_f)
    incident_f = rtf.act.incident
    drive(rtf, IncidentState.AWAITING_APPROVAL)
    fault_id = rtf.active_faults()[0]["id"]
    before = (
        rtf.manager.get(incident_f).state,
        [r.model_dump(mode="json") for r in approvals(rtf).records()],
        rtf.model_calls,
        rtf.active_faults(),
    )
    stranger_posts = [
        ("approve", {"proposal_hash": _latest_hash(rtf, incident_f)}),
        ("reject", {
            "proposal_hash": _latest_hash(rtf, incident_f), "notes": notes,
        }),
        ("reply", {"message": "hello"}),
        ("grant", {"turns": 4}),
        ("close", {"note": "bye"}),
    ]
    for action, body in stranger_posts:
        response = client_f.post(
            f"/api/incidents/{incident_f}/{action}", json=body
        )
        assert response.status_code == 403, action
        assert response.json() == {"error": "admin credential required"}
    response = client_f.post(f"/api/demo/faults/{fault_id}/clear")
    assert response.status_code == 403
    response = client_f.post("/api/demo/act", json={"act": "F2"})
    assert response.status_code == 403
    after = (
        rtf.manager.get(incident_f).state,
        [r.model_dump(mode="json") for r in approvals(rtf).records()],
        rtf.model_calls,
        rtf.active_faults(),
    )
    assert after == before
    assert host_f.current is rtf

    # The cookie: ?admin=t sets fleetsre_admin, which authorises a POST.
    host_c = make_host("F1")
    rtc = host_c.current
    client_c = make_client(host_c)
    incident_c = rtc.act.incident
    drive(rtc, IncidentState.AWAITING_APPROVAL)
    landed = client_c.get(f"/?admin={ADMIN_TOKEN}")
    assert landed.status_code == 200
    assert client_c.cookies.get("fleetsre_admin") == ADMIN_TOKEN
    authed = client_c.post(
        f"/api/incidents/{incident_c}/approve",
        json={"proposal_hash": _latest_hash(rtc, incident_c)},
    )
    assert authed.status_code == 200
    assert rtc.manager.get(incident_c).state == IncidentState.APPLYING

    stranger = make_client(host_c)
    denied = stranger.get("/?admin=wrong")
    assert denied.status_code == 200
    assert stranger.cookies.get("fleetsre_admin") is None
