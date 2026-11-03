"""D8 acceptance: make demo and make demo-check (d8-pins 7, Rows T6)."""

import json
import subprocess
import sys

from d8_helpers import ROOT, drive, make_client, make_host


def test_d8_t6_local_demo():
    """D8-T6: make demo/--check serve, trip fast and refuse --live with --check.

    (a) make -n shows python -m fleetsre.demo for both targets. (b) The real
    --check subprocess exits 0 with one RESOLVED ACT line per act and delay,
    modelCalls == scriptLength. (c) Each act trips within 300 x speed sim
    seconds of started_at. (d) GET / is 200 after start. (e) --check --live
    exits 2.
    """
    from fleetsre.control.incidents import IncidentState

    demo = subprocess.run(
        ["make", "-n", "demo"], capture_output=True, text=True, cwd=ROOT,
    )
    assert demo.returncode == 0, demo.stderr
    assert "python -m fleetsre.demo" in demo.stdout
    check = subprocess.run(
        ["make", "-n", "demo-check"], capture_output=True, text=True, cwd=ROOT,
    )
    assert check.returncode == 0, check.stderr
    assert "python -m fleetsre.demo" in check.stdout

    proc = subprocess.run(
        [sys.executable, "-m", "fleetsre.demo", "--check",
         "--delay", "0", "600"],
        capture_output=True, text=True, cwd=ROOT, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    runs = [
        json.loads(line[4:])
        for line in proc.stdout.splitlines()
        if line.startswith("ACT ")
    ]
    assert len(runs) == 6
    seen = set()
    for run in runs:
        assert run["state"] == "RESOLVED"
        assert run["modelCalls"] == run["scriptLength"]
        assert run["exhausted"] is False
        assert run["delay"] in (0, 600)
        seen.add((run["act"], run["delay"]))
    assert len(seen) == 6

    from fleetsre.demo.acts import load_acts

    acts = load_acts(str(ROOT / "fixtures" / "demo" / "acts.yaml"))
    for run in runs:
        assert run["incident"] == acts[run["act"]].incident
    targets = {
        "F1": IncidentState.AWAITING_APPROVAL,
        "F2": IncidentState.NEEDS_INPUT,
        "F3": IncidentState.AWAITING_APPROVAL,
    }
    for name, target in targets.items():
        host = make_host(name)
        rt = host.current
        drive(rt, target, cap_s=7200)
        first_trip_s = (rt.clock.now() - rt.started_at).total_seconds()
        assert first_trip_s <= 300 * 10.0, (name, first_trip_s)

    grid = make_client(make_host("F1")).get("/")
    assert grid.status_code == 200

    live = subprocess.run(
        [sys.executable, "-m", "fleetsre.demo", "--check", "--live"],
        capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    assert live.returncode == 2
