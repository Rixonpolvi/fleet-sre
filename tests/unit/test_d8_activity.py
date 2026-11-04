"""D8 activity unit collateral: live segment feed without rt.lock.

A scripted F1 segment with a transport hook snapshots activity mid-segment
(running with the turns so far) and idle afterwards; with rt.lock held by
another thread GET /api/demo/activity still answers within 1 s; --check
output is unchanged.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "acceptance" / "d8")
)

from d8_helpers import (  # noqa: E402
    drive,
    make_client,
    make_host,
)

from fleetsre.control.incidents import IncidentState  # noqa: E402


def test_activity_running_mid_segment_and_idle_after():
    host = make_host("F1")
    rt = host.current
    snapshots = []
    inner = rt.transport
    orig_create = inner.create

    def _hooked(request, timeout=120.0):
        snapshots.append(rt.activity.snapshot())
        return orig_create(request, timeout=timeout)

    inner.create = _hooked  # type: ignore[method-assign]
    try:
        record = drive(rt, IncidentState.AWAITING_APPROVAL)
    finally:
        inner.create = orig_create  # type: ignore[method-assign]
    assert snapshots, "transport hook never ran"
    for index, snap in enumerate(snapshots):
        assert snap["state"] == "running"
        assert snap["incident"] == rt.act.incident
        assert snap["deployment"] == record.incident.deployment
        assert snap["segment"] == "start"
        assert snap["trigger"] == "opened"
        assert snap["model"] == (
            rt.gateway.config.roles["investigate"].model
        )
        assert snap["tripwires"] == list(record.tripwires)
        assert isinstance(snap["started_real"], float)
        assert snap["elapsed_s"] >= 0.0
        assert isinstance(snap["call_in_flight"], bool)
        turns = snap["turns"]
        assert len(turns) == index
        for turn in turns:
            assert set(turn) == {"n", "tool", "rationale", "verdict"}
    assert snapshots[0]["turns"] == []
    assert len(snapshots[-1]["turns"]) == len(snapshots) - 1
    assert rt.activity.snapshot()["state"] == "idle"
    assert rt.activity.snapshot()["turns"] == []


def test_activity_endpoint_answers_while_lock_held():
    host = make_host("F1")
    rt = host.current
    client = make_client(host)
    acquired = threading.Event()
    release = threading.Event()

    def _hold():
        with rt.lock:
            acquired.set()
            assert release.wait(timeout=10.0)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        assert acquired.wait(timeout=10.0)
        start = time.time()
        response = client.get("/api/demo/activity")
        elapsed = time.time() - start
    finally:
        release.set()
        holder.join(timeout=10.0)
    assert response.status_code == 200
    assert elapsed < 1.0, elapsed
    body = response.json()
    assert body["state"] == "idle"
    assert body["tripwires"] == []
    assert set(body) == {
        "state", "incident", "deployment", "segment", "trigger",
        "model", "tripwires", "started_real", "elapsed_s",
        "call_in_flight", "turns",
    }


def test_check_output_unchanged():
    import inspect

    from fleetsre.demo.acts import load_acts
    from fleetsre.demo.host import DemoHost
    from fleetsre.demo.runtime import DemoRuntime
    from fleetsre.demo.transport import DemoTransport

    assert (
        inspect.signature(DemoRuntime.__init__)
        .parameters["turn_delay_s"].default == 0.0
    )
    assert (
        inspect.signature(DemoTransport.__init__)
        .parameters["turn_delay_s"].default == 0.0
    )
    assert (
        inspect.signature(DemoHost.__init__)
        .parameters["turn_delay_s"].default == 0.0
    )
    host = make_host("F1")
    rt = host.current
    assert rt.turn_delay_s == 0.0
    assert isinstance(rt.transport, DemoTransport)
    assert host.turn_delay_s == 0.0

    from fleetsre.demo.__main__ import _check_run

    acts = load_acts()
    run = _check_run("F1", acts["F1"], 0)
    assert run["state"] == "RESOLVED"
    assert run["incident"] == acts["F1"].incident
    assert run["modelCalls"] == run["scriptLength"]
    assert run["exhausted"] is False
    assert set(run) == {
        "act", "delay", "incident", "state", "modelCalls",
        "scriptLength", "exhausted", "refusals", "firstTripAt",
        "resolvedAt",
    }


def test_display_model_scripted_vs_live():
    from types import SimpleNamespace

    from fleetsre.web.views import display_model

    gateway = SimpleNamespace(config=SimpleNamespace(
        roles={"investigate": SimpleNamespace(model="muse-spark-1.3")}
    ))
    assert display_model(
        SimpleNamespace(live=False, gateway=gateway)
    ) == "scripted replay"
    assert display_model(
        SimpleNamespace(live=True, gateway=gateway)
    ) == "muse-spark-1.3"


def test_waiting_markers_in_pages_and_script():
    import re

    host = make_host("F1")
    rt = host.current
    client = make_client(host)
    drive(rt, IncidentState.AWAITING_APPROVAL)

    grid = client.get("/").text
    cards = re.findall(
        r'<article class="card dep-card" data-deployment="([^"]+)">',
        grid,
    )
    expected = [d.id for d in rt.evaluator.deployments]
    assert cards == expected

    page = client.get(f"/incidents/{rt.act.incident}").text
    assert (
        '<p id="sre-page-waiting" class="sre-page-waiting" hidden>'
        "The SRE is working; this page updates when it finishes</p>"
    ) in page
    assert 'data-live="0"' in page
    assert 'setAttribute("title", "SRE is working")' in page
    assert 'textContent = "SRE is working"' not in page
    assert "tripped on" in page
    assert '"scripted replay"' in page
    assert "sre-badge" in page
