"""D8 acceptance: turn timeline (d8-pins 14, Rows T9)."""

import re

import pytest

from d8_helpers import (
    ADMIN_HEADERS,
    READ_TOOLS,
    drive,
    elide_encrypted,
    make_client,
    make_host,
    transcripts,
)


def _expected_segments(session):
    """Segment per turn: 1 plus resumes before the turn (d8-pins 14)."""
    resumes = sorted(session.resumed_at_turns)
    return {
        turn: 1 + sum(1 for r in resumes if r < turn)
        for turn in range(1, session.turn + 1)
    }


def test_d8_t9_turn_timeline():
    """D8-T9: one card per turn, exact inputs, divider, verdict classes.

    F1 at AWAITING_APPROVAL: the cards match the transcript records (think
    is the call's rationale, act is tool and args without it), and the
    header equals the sums over the records. Every turn's input equals the
    real transport request with encrypted_content elided. After F3's
    rejection reopen a divider shows trigger rejection. The HTML carries
    one card element per turn with the verdict as a CSS class.
    """
    from fleetsre.control.incidents import IncidentState

    host = make_host("F1")
    rt = host.current
    client = make_client(host)
    incident_id = rt.act.incident
    drive(rt, IncidentState.AWAITING_APPROVAL)

    response = client.get(f"/api/incidents/{incident_id}")
    assert response.status_code == 200
    view = response.json()
    records = transcripts(rt, incident_id)
    assert len(records) > 0
    turns = view["turns"]
    assert len(turns) == len(records)
    session = rt.store.load(incident_id)
    segments = _expected_segments(session)
    for card, record in zip(turns, records):
        assert card["n"] == record["turn"]
        assert card["segment"] == segments[record["turn"]]
        assert card["verdict"] == record["verdict"]
        assert card["usage"] == record["usage"]
        if record["tool"] is None:
            assert card["think"] == ""
        else:
            assert card["think"] == record["args"].get("rationale", "")
            assert card["act"] == {
                "tool": record["tool"],
                "args": {
                    k: v for k, v in record["args"].items()
                    if k != "rationale"
                },
            }
        if record["verdict"] == "refused":
            assert card["observe"]["problems"] == record["problems"]
            assert "error" in card["observe"]
        elif record["tool"] is not None:
            assert card["observe"] == record["output"]["result"]

    header = view["header"]
    assert header["turns"] == len(records)
    assert header["model_calls"] == len(records)
    assert header["read_calls"] == sum(
        1 for r in records
        if r["verdict"] == "accepted" and r["tool"] in READ_TOOLS
    )
    for key in (
        "input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens",
    ):
        assert header["tokens"][key] == sum(r["usage"][key] for r in records)
    assert header["cost_usd"] == pytest.approx(
        sum(r["cost_usd"] for r in records)
    )
    expected_refusals: dict[str, int] = {}
    for record in records:
        if record["verdict"] == "refused":
            for problem in record["problems"]:
                rule = str(problem).split(":", 1)[0]
                expected_refusals[rule] = expected_refusals.get(rule, 0) + 1
    assert header["refusals"] == expected_refusals

    # Exact input: the real request, never a re-derivation (d8-pins §Review).
    requests = rt.transport.requests
    assert len(requests) == len(records)
    for card in turns:
        n = card["n"]
        got = client.get(f"/api/incidents/{incident_id}/turns/{n}/input")
        assert got.status_code == 200, n
        assert got.json() == elide_encrypted(requests[n - 1]["input"]), n

    # F3 after the rejection reopen: a divider with trigger rejection.
    host3 = make_host("F3")
    rt3 = host3.current
    client3 = make_client(host3)
    incident3 = rt3.act.incident
    drive(rt3, IncidentState.AWAITING_APPROVAL)
    proposal_hash = rt3.manager.get(incident3).proposals[-1].hash
    rejected = client3.post(
        f"/api/incidents/{incident3}/reject",
        json={
            "proposal_hash": proposal_hash,
            "notes": "unknown severities should map to 100",
        },
        headers=ADMIN_HEADERS,
    )
    assert rejected.status_code == 200
    rt3.advance(rt3.act.tick_seconds)
    assert rt3.manager.get(incident3).state == IncidentState.AWAITING_APPROVAL
    view3 = client3.get(f"/api/incidents/{incident3}").json()
    assert max(card["segment"] for card in view3["turns"]) == 2
    page3 = client3.get(f"/incidents/{incident3}")
    assert page3.status_code == 200
    assert re.search(
        r"segment-divider(.|\n){0,500}rejection", page3.text
    ), "no segment divider showing trigger rejection"

    # The HTML: one card element per turn, verdict as a CSS class.
    page = client.get(f"/incidents/{incident_id}")
    assert page.status_code == 200
    classes = [
        names.split()
        for names in re.findall(r'class="([^"]*)"', page.text)
        if "turn-card" in names.split()
    ]
    assert len(classes) == len(turns)
    got_verdicts = []
    for names in classes:
        verdicts = [v["verdict"] for v in turns]
        found = [name for name in names if name in verdicts]
        assert len(found) == 1, names
        got_verdicts.append(found[0])
    assert sorted(got_verdicts) == sorted(v["verdict"] for v in turns)
