#!/usr/bin/env python3
"""Record one live D5 investigation to fixtures/recorded/<FAULT>.json.

Rebuilds the scripted fixture's world, freezes it at the trip, runs one
live investigation, and writes {fault, seed, t0, faults, incident,
responses, transcript, diagnosis} (docs/d5-pins.md pin 32). Every
encrypted_content value is replaced with "<stripped>" at record time.

Usage: make record FAULT=<F1|F2|F3|F4|F5|F7>
Requires MODEL_API_KEY in the environment (never committed).
"""

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)


def _strip_encrypted(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: "<stripped>" if key == "encrypted_content"
            else _strip_encrypted(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_strip_encrypted(value) for value in node]
    return node


class RecordingTransport:
    """Wraps a transport, keeping deep copies of each model response."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.responses: list[dict[str, Any]] = []

    def create(
        self, request: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        response = self._inner.create(request, timeout=timeout)
        self.responses.append(copy.deepcopy(response))
        return response


def _live_config() -> tuple[Any, str]:
    from fleetsre.sre.gateway import GatewayConfig, RoleConfig

    model = os.environ.get(
        "FLEETSRE_INVESTIGATE_MODEL", "muse-spark-1.3"
    )
    prices: dict[str, dict[str, float]] = {
        model: {"input": 0.0, "cached_input": 0.0, "output": 0.0}
    }
    raw = os.environ.get("FLEETSRE_PRICES_JSON", "")
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            if model in data and isinstance(data[model], dict):
                prices = {model: dict(data[model])}
            elif "input" in data and "output" in data:
                prices = {model: dict(data)}
    return (
        GatewayConfig(
            roles={"investigate": RoleConfig(
                model=model, reasoning_effort="medium"
            )},
            base_url="https://api.meta.ai/v1",
            max_retries=5,
            prices=prices,
        ),
        model,
    )


def _trip_until(
    world: Any, evaluator: Any, clock: Any, sink: Any,
    deployment: str, tripwire: str,
) -> Any:
    consumed = 0
    elapsed = 0
    while elapsed <= 24 * 3600:
        tick_at = T0 + timedelta(seconds=elapsed)
        clock.set(tick_at)
        world.step_until(tick_at)
        events: list[Any] = []
        while consumed < len(sink.entries):
            events.extend(evaluator.ingest(sink.entries[consumed]))
            consumed += 1
        events.extend(evaluator.tick())
        for event in events:
            if (
                event.kind == "trip"
                and event.deployment == deployment
                and event.tripwire == tripwire
            ):
                clock.set(event.at)
                return event
        elapsed += 15
    raise AssertionError(f"no trip for {deployment} {tripwire}")


def main(argv: list[str] | None = None) -> int:
    from fastapi.testclient import TestClient

    from fleetsre.adapters.chronicle_http import ChronicleHttpClient
    from fleetsre.clock import SimClock
    from fleetsre.contracts import load_contract
    from fleetsre.evaluator.engine import Evaluator
    from fleetsre.mock.chronicle_api import create_app
    from fleetsre.mock.fleet import load_fleet
    from fleetsre.mock.world import World
    from fleetsre.ports import MemoryLogQuery, MemorySink, MemoryStateStore
    from fleetsre.registry import Registry, deployments_from_fleet
    from fleetsre.sre.gateway import (
        ModelGateway,
        OpenAITransport,
    )
    from fleetsre.sre.investigator import investigate
    from fleetsre.sre.schemas import Incident
    from fleetsre.sre.session import MemorySessionStore
    from fleetsre.sre.tools import ToolSet

    parser = argparse.ArgumentParser(description="Record a live D5 run")
    parser.add_argument("--fault", required=True,
                        help="fault ID, e.g. F1")
    args = parser.parse_args(argv)

    api_key = os.environ.get("MODEL_API_KEY", "")
    if not api_key:
        print("MODEL_API_KEY is not set; refusing a live run",
              file=sys.stderr)
        return 2

    fixture_path = ROOT / "fixtures" / "scripted" / f"{args.fault}.json"
    fixture = json.loads(fixture_path.read_text())
    deployment = fixture["incident"]["deployment"]
    service = deployment.split("/", 1)[0]

    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / f"{service}.json")
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(fleet, fixture["seed"], clock, sink)
    world.schedule(fixture["faults"])
    deployments = deployments_from_fleet(fleet, contract)
    evaluator = Evaluator([contract], deployments, clock)
    trip = _trip_until(
        world, evaluator, clock, sink, deployment,
        fixture["incident"]["tripwire"],
    )
    registry = Registry([contract], deployments)
    incident = Incident.from_trip(trip, registry, clock.now())

    log_query = MemoryLogQuery(sink)
    http: Any = TestClient(create_app(world, dev_auth=True))
    chronicle = ChronicleHttpClient(http, "obs-reader")
    state = MemoryStateStore()
    tools = ToolSet(
        log_query, chronicle, state, registry,
        clock=clock, verification=True,
    )
    config, model = _live_config()
    recording = RecordingTransport(OpenAITransport(
        api_key=api_key, base_url="https://api.meta.ai/v1",
    ))
    gateway = ModelGateway(config, recording)
    store = MemorySessionStore()
    result = investigate(
        incident, tools, gateway, store, max_turns=24,
        max_read_calls=12, max_tokens=400_000,
    )
    payload = {
        "fault": args.fault,
        "seed": fixture["seed"],
        "t0": fixture["t0"],
        "faults": fixture["faults"],
        "incident": incident.model_dump(mode="json"),
        "responses": recording.responses,
        "transcript": store.transcript(result.transcript_id),
        "diagnosis": result.diagnosis,
    }
    payload = _strip_encrypted(payload)
    out_dir = ROOT / "fixtures" / "recorded"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.fault}.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    category = (result.diagnosis or {}).get("category", None)
    print(f"model: {model}")
    print(f"status: {result.status} reason: {result.reason}")
    print(f"turns: {result.turns} read_calls: {result.read_calls}")
    print(f"category: {category}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
