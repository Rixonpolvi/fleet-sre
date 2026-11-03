"""D8 story unit collateral: banner, linear story and wired buttons.

Drives F1, F2, F3 like the d8 acceptance helpers and asserts next_step
and the story actor/kind sequence at AWAITING_APPROVAL, NEEDS_INPUT
(before and after clear_fault) and RESOLVED. F3's story carries the
rejection notes verbatim, a queued reply shows, every admin button has
data-url, and the inline script passes node --check when node exists.
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
from fleetsre.web.views import next_step, story  # noqa: E402


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
        ("sre", "segment"),
        ("sre", "proposal"),
    ]
    assert "no connector.run.completed" in entries[0]["text"]
    proposal = record.proposals[0]
    diagnosis = proposal.diagnosis
    assert diagnosis["category"] in entries[2]["text"]
    assert diagnosis["summary"] in entries[2]["text"]
    assert proposal.method in entries[2]["text"]
    for entry in entries:
        assert "mark rotated" not in entry["text"].lower()


def test_f2_needs_before_and_after_clear():
    host = make_host("F2")
    rt = host.current
    record = drive(rt, IncidentState.NEEDS_INPUT)
    step = next_step(rt, record)
    assert step["actor"] == "you"
    assert step["text"] == (
        "fix it outside the platform (Mark rotated), reply, or close"
    )
    entries = story(rt, record)
    assert _seq(entries) == [
        ("tripwire", "opened"),
        ("sre", "segment"),
        ("sre", "needs_input"),
    ]
    assert "escalated" in entries[2]["text"]
    assert record.diagnosis["action"]["escalationSummary"] in entries[2]["text"]

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
        ("sre", "segment"),
        ("sre", "needs_input"),
        ("you", "demo"),
    ]
    assert entries[3]["text"] == "rotated the credential outside the platform"
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
        ("sre", "segment"),
        ("sre", "needs_input"),
        ("you", "demo"),
        ("tripwire", "recovery"),
        ("tripwire", "resumed"),
        ("sre", "segment"),
        ("platform", "verification_started"),
        ("tripwire", "verified"),
    ]
    resumed = entries[5]
    assert "resumed the investigation" in resumed["text"]
    assert record.postmortem.closed_at.isoformat() in step["text"]
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
        ("sre", "segment"),
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
        ("sre", "segment"),
        ("sre", "proposal"),
        ("you", "rejected"),
        ("sre", "resumed"),
        ("sre", "segment"),
        ("sre", "proposal"),
    ]
    assert notes in entries[3]["text"]
    assert entries[4]["text"] == "revising after your rejection"
    first = record.proposals[0].diagnosis["summary"]
    second = record.proposals[1].diagnosis["summary"]
    assert first != second
    assert first in entries[2]["text"]
    assert second in entries[6]["text"]
    assert notes in entries[3]["text"]
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
    assert len(entries) == len(record.timeline) + 1
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
