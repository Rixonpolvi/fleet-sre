"""D3 speed: 6 simulated hours for the full fleet step in under 2 s."""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleetsre.clock import SimClock
from fleetsre.mock.fleet import load_fleet
from fleetsre.mock.world import World
from fleetsre.ports import MemorySink

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)


def test_world_speed_six_hours(monkeypatch):
    monkeypatch.setenv("PROJECT", "fleet-sre-test")
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(fleet, 7, clock, sink)
    started = time.perf_counter()
    world.step_until(T0 + timedelta(seconds=6 * 3600))
    elapsed = time.perf_counter() - started
    assert len(sink.entries) > 0
    print(f"\n6 simulated hours stepped in {elapsed:.3f}s")
    assert elapsed < 2.0
