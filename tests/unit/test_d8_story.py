"""D8 story unit collateral: banner, linear story and wired buttons.

Drives F1, F2, F3 like the d8 acceptance helpers and asserts next_step
and the story actor/kind sequence at AWAITING_APPROVAL, NEEDS_INPUT
(before and after clear_fault) and RESOLVED. F3's story carries the
rejection notes verbatim, a queued reply shows, every admin button has
data-url, and the inline script passes node --check when node exists.
Story2: one entry per SRE segment with headers, transcript diagnoses,
per-segment evidence, fast model_unavailable handoff.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "acceptance" / "d8")
)

from d8_helpers import (  # noqa: E402
    ADMIN_HEADERS,
    ADMIN_TOKEN,
    drive,
    make_client,
    make_host,
)

from fleetsre.control.incidents import IncidentState  # noqa: E402
from fleetsre.web.views import handoff_actions, next_step, story  # noqa: E402


def _seq(entries):
    return [(entry["actor"], entry["kind"]) for entry in entries]


def test_f1_awaiting_next_and_story():
    host = make_host("F1")
    rt = host.current
    record = drive(rt, IncidentState.AWAITING_APPROVAL)
    step = next_step(rt, record)
    assert step == {"actor": "you", "text": "approve or reject proposal #1"}
    entries = story(rt, record)
    assert _seq(entries) == [
        ("tripwire", "opened"),
        ("sre", "proposal"),
    ]
    assert "no connector.run.completed" in entries[0]["text"]
    assert entries[1]["text"].startswith(
        "SRE (scripted replay) investigated \u00b7 9 turns"
    )
    proposal = record.proposals[0]
    diagnosis = proposal.diagnosis
    assert diagnosis["category"] in entries[1]["text"]
    assert diagnosis["summary"] in entries[1]["text"]
    assert proposal.method in entries[1]["text"]
    for entry in entries:
        assert "mark rotated" not in entry["text"].lower()


def test_f2_needs_before_and_after_clear():
    host = make_host("F2")
    rt = host.current
    record = drive(rt, IncidentState.NEEDS_INPUT)
    step = next_step(rt, record)
    assert step["actor"] == "you"
    diagnosis = record.diagnosis
    assert step["text"] == (
        "The SRE found credential failure on bravo "
        f"({diagnosis['summary']}). "
        "The fix is outside what the SRE may do. "
        f"{diagnosis['action']['escalationSummary']}"
    )
    entries = story(rt, record)
    assert _seq(entries) == [
        ("tripwire", "opened"),
        ("sre", "needs_input"),
    ]
    assert entries[1]["text"].startswith(
        "SRE (scripted replay) investigated \u00b7 9 turns"
    )
    assert "escalated" in entries[1]["text"]
    assert record.diagnosis["action"]["escalationSummary"] in entries[1]["text"]

    fault_id = rt.active_faults()[0]["id"]
    rt.clear_fault(fault_id)
    cleared = rt.manager.get(rt.act.incident)
    demo_detail = [
        event.detail for event in cleared.timeline if event.kind == "demo"
    ][0]
    assert demo_detail["by"] == "operator"
    step = next_step(rt, cleared)
    assert step["actor"] == "tripwire"
    assert "credential rotated at" in step["text"]
    assert "to recover, then the SRE resumes" in step["text"]
    assert "connector-errors" in step["text"]
    entries = story(rt, cleared)
    assert _seq(entries) == [
        ("tripwire", "opened"),
        ("sre", "needs_input"),
        ("you", "demo"),
    ]
    assert entries[2]["text"] == "rotated the credential outside the platform"
    for entry in entries:
        assert "mark rotated" not in entry["text"].lower()


def test_f2_resolved_next_and_story():
    host = make_host("F2")
    rt = host.current
    drive(rt, IncidentState.NEEDS_INPUT)
    rt.clear_fault(rt.active_faults()[0]["id"])
    record = drive(rt, IncidentState.RESOLVED)
    step = next_step(rt, record)
    assert step["actor"] == "done"
    assert step["text"].startswith("RESOLVED at ")
    entries = story(rt, record)
    assert _seq(entries) == [
        ("tripwire", "opened"),
        ("sre", "needs_input"),
        ("you", "demo"),
        ("tripwire", "recovery"),
        ("sre", "segment"),
        ("platform", "verification_started"),
        ("tripwire", "verified"),
    ]
    sre_entries = [entry for entry in entries if entry["actor"] == "sre"]
    assert len(sre_entries) == 2
    first, second = sre_entries
    assert first["text"].startswith(
        "SRE (scripted replay) investigated \u00b7 9 turns"
    )
    assert "Fix is outside the allowlist; needs a human." in first["text"]
    assert second["text"].startswith(
        "SRE resumed after the tripwire recovered"
    )
    assert "Re-checked" in second["text"]
    assert record.postmortem.closed_at.strftime("%H:%M:%S") in step["text"]
    assert record.postmortem.closed_at.isoformat() not in step["text"]
    for entry in entries:
        assert "mark rotated" not in entry["text"].lower()


def test_f1_resolved_next_and_story():
    host = make_host("F1")
    rt = host.current
    client = make_client(host)
    drive(rt, IncidentState.AWAITING_APPROVAL)
    proposal_hash = rt.manager.get(rt.act.incident).proposals[-1].hash
    approved = client.post(
        f"/api/incidents/{rt.act.incident}/approve",
        json={"proposal_hash": proposal_hash},
        headers=ADMIN_HEADERS,
    )
    assert approved.status_code == 200
    record = drive(rt, IncidentState.RESOLVED)
    step = next_step(rt, record)
    assert step["actor"] == "done"
    assert step["text"].startswith("RESOLVED at ")
    entries = story(rt, record)
    assert _seq(entries) == [
        ("tripwire", "opened"),
        ("sre", "proposal"),
        ("you", "approved"),
        ("platform", "applied"),
        ("platform", "verification_started"),
        ("tripwire", "recovery"),
        ("tripwire", "verified"),
    ]


def test_f3_rejection_notes_verbatim_and_revise():
    host = make_host("F3")
    rt = host.current
    client = make_client(host)
    drive(rt, IncidentState.AWAITING_APPROVAL)
    notes = "unknown severities should map to 100"
    proposal_hash = rt.manager.get(rt.act.incident).proposals[-1].hash
    rejected = client.post(
        f"/api/incidents/{rt.act.incident}/reject",
        json={"proposal_hash": proposal_hash, "notes": notes},
        headers=ADMIN_HEADERS,
    )
    assert rejected.status_code == 200
    rt.advance(rt.act.tick_seconds)
    record = rt.manager.get(rt.act.incident)
    assert record.state == IncidentState.AWAITING_APPROVAL
    entries = story(rt, record)
    assert _seq(entries) == [
        ("tripwire", "opened"),
        ("sre", "proposal"),
        ("you", "rejected"),
        ("sre", "proposal"),
    ]
    assert notes in entries[2]["text"]
    assert entries[3]["text"].startswith("SRE resumed after your rejection")
    first = record.proposals[0].diagnosis["summary"]
    second = record.proposals[1].diagnosis["summary"]
    assert first != second
    assert first in entries[1]["text"]
    assert second in entries[3]["text"]
    assert notes in entries[2]["text"]
    for entry in entries:
        assert "mark rotated" not in entry["text"].lower()


def test_queued_reply_shows():
    host = make_host("F2")
    rt = host.current
    client = make_client(host)
    drive(rt, IncidentState.NEEDS_INPUT)
    message = "rotated by hand"
    reply = client.post(
        f"/api/incidents/{rt.act.incident}/reply",
        json={"message": message},
        headers=ADMIN_HEADERS,
    )
    assert reply.status_code == 200
    record = rt.manager.get(rt.act.incident)
    assert record.state == IncidentState.INVESTIGATING
    step = next_step(rt, record)
    assert step == {
        "actor": "sre", "text": "resumes on the next tick: reply",
    }
    entries = story(rt, record)
    assert _seq(entries) == [
        ("tripwire", "opened"),
        ("sre", "needs_input"),
        ("you", "reply"),
    ]
    queued = entries[-1]
    assert queued["actor"] == "you"
    assert message in queued["text"]
    assert "queued; the SRE resumes on the next tick" in queued["text"]


def test_admin_buttons_wired_with_data_url():
    host = make_host("F1")
    rt = host.current
    client = make_client(host)
    drive(rt, IncidentState.AWAITING_APPROVAL)
    page = client.get(
        f"/incidents/{rt.act.incident}?admin={ADMIN_TOKEN}"
    ).text
    assert 'id="btn-approve"' in page
    assert 'id="btn-reject"' in page
    assert 'id="action-error"' in page
    for button in re.findall(r"<button[^>]*>", page):
        assert "data-url" in button, button
        assert 'data-method="POST"' in button, button
    assert page.count('addEventListener("click"') == 1

    host2 = make_host("F2")
    rt2 = host2.current
    client2 = make_client(host2)
    drive(rt2, IncidentState.NEEDS_INPUT)
    admin = client2.get(
        f"/incidents/{rt2.act.incident}?admin={ADMIN_TOKEN}"
    ).text
    assert "data-clear-url" in admin
    assert "Mark rotated" in admin
    for button in re.findall(r"<button[^>]*>", admin):
        assert "data-url" in button, button
        assert 'data-method="POST"' in button, button
    plain = make_client(host2).get(f"/incidents/{rt2.act.incident}").text
    assert "/api/demo/faults/" not in plain
    assert "mark rotated" not in plain.lower()


def test_inline_script_passes_node_check():
    node = shutil.which("node")
    if node is None:
        return
    host = make_host("F1")
    rt = host.current
    client = make_client(host)
    drive(rt, IncidentState.AWAITING_APPROVAL)
    page = client.get(
        f"/incidents/{rt.act.incident}?admin={ADMIN_TOKEN}"
    ).text
    scripts = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
    assert scripts
    for script in scripts:
        if not script.strip():
            continue
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False
        ) as handle:
            handle.write(script)
            name = handle.name
        try:
            proc = subprocess.run(
                [node, "--check", name],
                capture_output=True, text=True, timeout=30,
            )
        finally:
            Path(name).unlink(missing_ok=True)
        assert proc.returncode == 0, proc.stderr


def test_f2_segments_use_their_own_diagnoses_and_disjoint_evidence():
    host = make_host("F2")
    rt = host.current
    drive(rt, IncidentState.NEEDS_INPUT)
    rt.clear_fault(rt.active_faults()[0]["id"])
    record = drive(rt, IncidentState.RESOLVED)
    entries = story(rt, record)
    sre_entries = [entry for entry in entries if entry["actor"] == "sre"]
    assert len(sre_entries) == 2
    first, second = sre_entries
    seg1_summary = "The bravo connector credential expired; upstream 401s."
    seg2_summary = (
        "The bravo connector credential had expired; rotation restored it."
    )
    assert seg1_summary in first["text"]
    assert "Fix is outside the allowlist; needs a human." in first["text"]
    assert seg2_summary not in first["text"]
    assert second["text"].startswith(
        "SRE resumed after the tripwire recovered"
    )
    assert "Re-checked" in second["text"]
    assert seg2_summary in second["text"]
    first_ids = {item["id"] for item in first["evidence"]}
    second_ids = {item["id"] for item in second["evidence"]}
    assert first_ids == {"E1", "E2", "E3"}
    assert second_ids == {"E4"}
    assert first_ids.isdisjoint(second_ids)


def test_f3_segments_carry_proposals_and_disjoint_evidence():
    host = make_host("F3")
    rt = host.current
    client = make_client(host)
    drive(rt, IncidentState.AWAITING_APPROVAL)
    proposal_hash = rt.manager.get(rt.act.incident).proposals[-1].hash
    rejected = client.post(
        f"/api/incidents/{rt.act.incident}/reject",
        json={
            "proposal_hash": proposal_hash,
            "notes": "unknown severities should map to 100",
        },
        headers=ADMIN_HEADERS,
    )
    assert rejected.status_code == 200
    rt.advance(rt.act.tick_seconds)
    record = rt.manager.get(rt.act.incident)
    entries = story(rt, record)
    sre_entries = [entry for entry in entries if entry["actor"] == "sre"]
    assert len(sre_entries) == 2
    assert [entry["change"]["seq"] for entry in sre_entries] == [1, 2]
    assert "Proposed #1" in sre_entries[0]["text"]
    assert "Proposed #2" in sre_entries[1]["text"]
    assert sre_entries[1]["text"].startswith(
        "SRE resumed after your rejection"
    )
    first_ids = {item["id"] for item in sre_entries[0]["evidence"]}
    second_ids = {item["id"] for item in sre_entries[1]["evidence"]}
    assert first_ids == {"E1", "E2", "E3"}
    assert second_ids == {"E4"}
    assert first_ids.isdisjoint(second_ids)


def test_live_config_fails_fast_on_hung_call():
    from fleetsre.demo.runtime import _live_config
    from fleetsre.sre.gateway import (
        GatewayConfig,
        ModelGateway,
        RoleConfig,
        TransportError,
    )

    config = _live_config()
    role = config.roles["investigate"]
    assert role.timeout_s == 60
    assert role.retry_timeouts is False

    class _HungTransport:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, request, *, timeout):
            self.calls += 1
            assert timeout == 60
            raise TransportError(504, "timed out", timeout=True)

    hung = _HungTransport()
    slept: list[float] = []
    gateway = ModelGateway(
        GatewayConfig(
            roles={"investigate": RoleConfig(
                model="m", reasoning_effort="low",
                timeout_s=60, retry_timeouts=False,
            )},
            base_url="https://x",
            max_retries=5,
        ),
        hung,
        sleep=slept.append,
    )
    try:
        gateway.respond("investigate", [], None)
    except TransportError as exc:
        assert exc.timeout is True
    else:
        raise AssertionError("hung call should raise")
    assert hung.calls == 1
    assert slept == []

    attempts = {"count": 0}

    class _FlakyTransport:
        def create(self, request, *, timeout):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise TransportError(429, "rate limited")
            return {"output": [], "usage": {}}

    slept2: list[float] = []
    gateway2 = ModelGateway(
        GatewayConfig(
            roles={"investigate": RoleConfig(
                model="m", reasoning_effort="low",
                timeout_s=60, retry_timeouts=False,
            )},
            base_url="https://x",
            max_retries=5,
        ),
        _FlakyTransport(),
        sleep=slept2.append,
    )
    gateway2.respond("investigate", [], None)
    assert attempts["count"] == 2
    assert slept2 == [1.0]


def test_model_unavailable_handoff_prefills_retry_note():
    host = make_host("F2")
    rt = host.current
    client = make_client(host)
    drive(rt, IncidentState.NEEDS_INPUT)
    record = rt.manager.get(rt.act.incident)
    assert bool(rt.transport.exhausted) is False
    record.reason = "model_unavailable"
    step = next_step(rt, record)
    assert step["text"] == "The model did not answer in time"
    actions = handoff_actions(record, rt)
    assert actions is not None
    assert actions["primary"] == "note"
    assert actions["note_prefill"] == "Retry."
    page = client.get(
        f"/incidents/{rt.act.incident}?admin={ADMIN_TOKEN}"
    ).text
    assert "did not answer in time" in page
    assert "Send the SRE a note" in page
    assert 'value="Retry."' in page
