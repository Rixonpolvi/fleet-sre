"""D8 acceptance: handoff card (d8-pins 15, Rows T10)."""

from d8_helpers import (
    ADMIN_HEADERS,
    ADMIN_TOKEN,
    drive,
    make_client,
    make_host,
    transcripts,
)


def _incident(client, incident_id):
    response = client.get(f"/api/incidents/{incident_id}")
    assert response.status_code == 200
    return response.json()


def _needs_input_f2():
    host = make_host("F2")
    rt = host.current
    client = make_client(host)
    from fleetsre.control.incidents import IncidentState

    drive(rt, IncidentState.NEEDS_INPUT)
    return host, rt, client


def test_d8_t10_handoff_card():
    """D8-T10: NEEDS_INPUT handoff, quiet wait, mark rotated, reply/grant/close.

    F2 at NEEDS_INPUT carries reason escalated, the session's hypotheses
    and evidence, a non-empty escalation summary, and the mark-rotated
    control in the admin HTML only. After 3600 s of quiet no model call
    was made. Clearing the credential_expiry fault resolves with action
    none. Fresh pauses: reply resumes without ticking, grant adds turns,
    close escalates with a postmortem.
    """
    from fleetsre.control.incidents import IncidentState

    host, rt, client = _needs_input_f2()
    incident_id = rt.act.incident
    view = _incident(client, incident_id)
    handoff = view["handoff"]
    assert handoff is not None
    assert handoff["reason"] == "escalated"
    session = rt.store.load(incident_id)
    assert [h["id"] for h in handoff["hypotheses"]] == list(session.hypotheses)
    assert len(handoff["evidence"]) == len(session.evidence)
    for item in handoff["evidence"]:
        recorded = session.evidence[item["id"]]
        assert item["hypothesis"] == recorded.hypothesis
        assert item["stance"] == recorded.stance
    record = rt.manager.get(incident_id)
    assert handoff["escalation_summary"] == (
        record.diagnosis["action"]["escalationSummary"]
    )
    assert handoff["escalation_summary"]
    last_refused = next(
        (r for r in reversed(transcripts(rt, incident_id))
         if r["verdict"] == "refused"),
        None,
    )
    if last_refused is None:
        assert handoff["last_refusal"] is None
    else:
        assert handoff["last_refusal"] == {
            "n": last_refused["turn"],
            "tool": last_refused["tool"],
            "problems": last_refused["problems"],
        }

    faults = rt.active_faults()
    assert len(faults) == 1
    assert faults[0]["type"] == "credential_expiry"
    assert faults[0]["environment"] == "bravo"
    clear_url = f"/api/demo/faults/{faults[0]['id']}/clear"
    admin_page = client.get(f"/incidents/{incident_id}?admin={ADMIN_TOKEN}")
    assert admin_page.status_code == 200
    assert clear_url in admin_page.text
    assert "mark rotated" in admin_page.text.lower()
    plain_page = make_client(host).get(f"/incidents/{incident_id}")
    assert plain_page.status_code == 200
    assert "/api/demo/faults/" not in plain_page.text
    assert "mark rotated" not in plain_page.text.lower()

    calls_before = rt.model_calls
    rt.advance(3600)
    assert rt.model_calls == calls_before
    assert rt.manager.get(incident_id).state == IncidentState.NEEDS_INPUT
    waiting = _incident(client, incident_id)
    assert waiting["handoff"]["model_calls_since_pause"] == 0

    cleared = client.post(clear_url, headers=ADMIN_HEADERS)
    assert cleared.status_code == 200
    assert all(
        f["id"] != faults[0]["id"] for f in rt.active_faults()
    )
    drive(rt, IncidentState.RESOLVED)
    assert rt.manager.get(incident_id).diagnosis["action"]["type"] == "none"

    _, rt_reply, client_reply = _needs_input_f2()
    incident_reply = rt_reply.act.incident
    tick_before = rt_reply.clock.now()
    reply = client_reply.post(
        f"/api/incidents/{incident_reply}/reply",
        json={"message": "rotated by hand"},
        headers=ADMIN_HEADERS,
    )
    assert reply.status_code == 200
    record_reply = rt_reply.manager.get(incident_reply)
    assert record_reply.state == IncidentState.INVESTIGATING
    assert record_reply.pending is not None
    assert record_reply.pending.message == (
        "Reply from operator: rotated by hand"
    )
    assert rt_reply.clock.now() == tick_before

    _, rt_grant, client_grant = _needs_input_f2()
    incident_grant = rt_grant.act.incident
    granted = client_grant.post(
        f"/api/incidents/{incident_grant}/grant",
        json={"turns": 4},
        headers=ADMIN_HEADERS,
    )
    assert granted.status_code == 200
    record_grant = rt_grant.manager.get(incident_grant)
    assert record_grant.state == IncidentState.INVESTIGATING
    assert record_grant.pending is not None
    assert record_grant.pending.extra_turns == 4

    _, rt_close, client_close = _needs_input_f2()
    incident_close = rt_close.act.incident
    closed = client_close.post(
        f"/api/incidents/{incident_close}/close",
        json={"note": "handing to netops"},
        headers=ADMIN_HEADERS,
    )
    assert closed.status_code == 200
    record_close = rt_close.manager.get(incident_close)
    assert record_close.state == IncidentState.ESCALATED
    assert record_close.postmortem is not None
