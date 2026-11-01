"""D6 acceptance helpers: E2E world/ToolSet/ScriptedTransport setup (d6a-pins 22).

Timing numbers used by the tests were pre-flighted with the implemented D4
replay loop (same seed, t0, schedule, 15 s ticks, canary scheduler on):
first trips match the D5 scripted fixtures' incidents exactly, and each
recovery delay below was measured there (see the D6a tests session report).
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)
TICK = 15

D5_FAULTS = ("F1", "F2", "F3", "F4", "F5", "F7")

CONNECTOR_TMPL = ("projects/fleet-sre-demo/locations/us/instances/mock-soar"
                  "/integrations/MockSentinel/connectors/"
                  "mock-sentinel-incidents/connectorInstances/{}")
JOB_TMPL = ("projects/fleet-sre-demo/locations/us/instances/mock-soar"
            "/integrations/MockThreatIntel/jobs/sync-indicators/"
            "jobInstances/{}")
INTEG_TMPL = ("projects/fleet-sre-demo/locations/us/instances/mock-soar"
              "/integrations/MockSentinel/integrationInstances/{}")


def at_offset(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def load_d5(fault: str) -> dict[str, Any]:
    path = ROOT / "fixtures" / "scripted" / f"{fault}.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing D5 scripted fixture: {path}")
    return json.loads(path.read_text())


def load_d6(name: str) -> dict[str, Any]:
    path = ROOT / "fixtures" / "scripted" / "d6" / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing D6 scripted fixture: {path}")
    return json.loads(path.read_text())


def primary_fixture(name: str) -> dict[str, Any]:
    """Segment-1 fixture: a D5 fault, or F3-reject from fixtures/scripted/d6."""
    if name in D5_FAULTS:
        return load_d5(name)
    return load_d6(name)


def contract(service: str) -> Any:
    from fleetsre.contracts import load_contract

    return load_contract(ROOT / "contracts" / f"{service}.json")


def fleet() -> Any:
    from fleetsre.mock.fleet import load_fleet

    return load_fleet(ROOT / "fixtures" / "fleet.yaml")


def connector_name(flt: Any, env: str) -> str:
    inst = flt.environments[env].deployments["alert-ingestion"].connectorInstance
    return CONNECTOR_TMPL.format(inst)


def job_name(flt: Any, env: str) -> Any:
    inst = flt.environments[env].deployments["intel-sync"].jobInstance
    return JOB_TMPL.format(inst)


def gateway_config(
    model: str = "test-model",
    effort: str = "medium",
    max_retries: int = 5,
) -> Any:
    from fleetsre.sre.gateway import GatewayConfig, RoleConfig

    return GatewayConfig(
        roles={"investigate": RoleConfig(
            model=model, reasoning_effort=effort
        )},
        base_url="https://api.meta.ai/v1",
        max_retries=max_retries,
        prices={model: {
            "input": 1.0, "cached_input": 0.5, "output": 2.0,
        }},
    )


class RecordingChronicle:
    """ChronicleClient wrapper recording every write call (d6a-pins 22/25)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.writes: list[tuple[str, str]] = []

    @property
    def identity(self) -> str:
        return self.inner.identity

    def get(self, name: str) -> dict[str, Any]:
        return self.inner.get(name)

    def list_connector_logs(
        self, name: str, page_size: int
    ) -> dict[str, Any]:
        return self.inner.list_connector_logs(name, page_size)

    def list_job_logs(self, name: str, page_size: int) -> dict[str, Any]:
        return self.inner.list_job_logs(name, page_size)

    def execute_test(self, name: str) -> dict[str, Any]:
        return self.inner.execute_test(name)

    def patch(
        self, name: str, body: dict[str, Any], update_mask: list[str]
    ) -> dict[str, Any]:
        self.writes.append(("patch", name))
        return self.inner.patch(name, body, update_mask)

    def run_on_demand(
        self, name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self.writes.append(("runOnDemand", name))
        return self.inner.run_on_demand(name, body)

    def set_logs_collection(
        self, name: str, enabled: bool
    ) -> dict[str, Any]:
        self.writes.append(("setLogsCollection", name))
        return self.inner.set_logs_collection(name, enabled)


def update_plan_script(first: int, count: int) -> list[dict[str, Any]]:
    """Inline script of `count` update_plan turns (for budget pauses)."""
    out = []
    for turn in range(first, first + count):
        out.append({
            "output": [
                {"type": "reasoning", "id": f"rs_{turn:03d}",
                 "encrypted_content": f"enc-placeholder-{turn}"},
                {"type": "function_call", "call_id": f"call_{turn:03d}",
                 "name": "update_plan",
                 "arguments": json.dumps({
                     "plan": [], "rationale": f"stall turn {turn}",
                 })},
            ],
            "usage": {"input_tokens": 1000, "output_tokens": 50,
                      "input_tokens_details": {"cached_tokens": 600},
                      "output_tokens_details": {"reasoning_tokens": 10}},
        })
    return out


def last_user_text(request: dict[str, Any]) -> str:
    """Text of the last user-role input item of a model request."""
    for item in reversed(request.get("input", [])):
        if isinstance(item, dict) and item.get("role") == "user":
            return str(item.get("content", ""))
    return ""


def build_e2e(
    primary: str,
    *segments: str,
    investigator_options: dict[str, Any] | None = None,
    extra_responses: list[dict[str, Any]] | None = None,
    record_writes: bool = False,
    first: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Seeded World + Evaluator + scheduler + manager + loop (d6a-pins 22).

    `primary` is a D5 fault (F1..F4) or F3-reject; `segments` are d6
    fixture names whose responses follow segment 1 in one
    ScriptedTransport. `first` replaces the segment-1 responses (for
    budget-pause scripts). Fails with NotImplementedError on the D6a stubs.
    """
    from fastapi.testclient import TestClient

    from fleetsre.adapters.chronicle_http import ChronicleHttpClient
    from fleetsre.clock import SimClock
    from fleetsre.control.approvals import ApprovalStore
    from fleetsre.control.executor import Executor
    from fleetsre.control.incidents import IncidentManager
    from fleetsre.control.journal import Journal
    from fleetsre.control.loop import ControlLoop
    from fleetsre.control.verify import Verifier
    from fleetsre.evaluator.canary import CanaryScheduler
    from fleetsre.evaluator.engine import Evaluator
    from fleetsre.mock.chronicle_api import create_app
    from fleetsre.mock.world import World
    from fleetsre.ports import (
        MemoryLogQuery,
        MemorySink,
        MemoryStateStore,
    )
    from fleetsre.registry import Registry, deployments_from_fleet
    from fleetsre.sre.gateway import ModelGateway, ScriptedTransport
    from fleetsre.sre.investigator import Investigator
    from fleetsre.sre.session import MemorySessionStore
    from fleetsre.sre.tools import ToolSet

    fixture = primary_fixture(primary)
    service = fixture["incident"]["deployment"].split("/", 1)[0]
    flt = fleet()
    cont = contract(service)
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(flt, fixture["seed"], clock, sink)
    world.schedule(fixture["faults"])
    deployments = deployments_from_fleet(flt, cont)
    evaluator = Evaluator([cont], deployments, clock)
    scheduler = CanaryScheduler(world, evaluator, [cont], clock)
    responses = list(first) if first is not None else list(
        fixture["responses"]
    )
    for name in segments:
        responses.extend(load_d6(name)["responses"])
    if extra_responses:
        responses.extend(extra_responses)
    transport = ScriptedTransport(responses)
    gateway = ModelGateway(gateway_config(), transport)
    log_query = MemoryLogQuery(sink)
    http = TestClient(create_app(world, dev_auth=True))
    obs: Any = ChronicleHttpClient(http, "obs-reader")
    deployer: Any = ChronicleHttpClient(http, "deployer")
    reader: Any = ChronicleHttpClient(http, "obs-reader")
    recorders: list[RecordingChronicle] = []
    if record_writes:
        obs = RecordingChronicle(obs)
        deployer = RecordingChronicle(deployer)
        reader = RecordingChronicle(reader)
        recorders = [obs, deployer, reader]
    state = MemoryStateStore()
    registry = Registry([cont], deployments)
    tools = ToolSet(
        log_query, obs, state, registry, clock=clock, verification=True,
    )
    store = MemorySessionStore()
    investigator = Investigator(
        tools, gateway, store, **(investigator_options or {})
    )
    approvals = ApprovalStore(clock)
    journal = Journal()
    executor = Executor(deployer, approvals, journal, clock=clock)
    verifier = Verifier(evaluator, registry, clock)
    manager = IncidentManager(
        evaluator, investigator, approvals, executor, verifier, state,
        clock, registry=registry, reader=reader,
    )
    loop = ControlLoop(
        world, evaluator, scheduler, manager, sink, t0=T0,
        tick_seconds=TICK,
    )
    return {
        "fixture": fixture, "fleet": flt, "contract": cont,
        "clock": clock, "sink": sink, "world": world,
        "deployments": deployments, "evaluator": evaluator,
        "scheduler": scheduler, "transport": transport,
        "gateway": gateway, "tools": tools, "store": store,
        "investigator": investigator, "approvals": approvals,
        "journal": journal, "executor": executor,
        "verifier": verifier, "manager": manager, "loop": loop,
        "obs": obs, "deployer": deployer, "reader": reader,
        "state": state, "registry": registry,
        "recorders": recorders,
    }


def step_until_incidents(
    e2e: dict[str, Any], count: int, bound_offset: int
) -> None:
    """Step the loop until `count` incidents exist or the bound passes."""
    manager = e2e["manager"]
    loop = e2e["loop"]
    clock = e2e["clock"]
    bound = at_offset(bound_offset)
    while len(manager.incidents()) < count and clock.now() < bound:
        loop.step()
    assert len(manager.incidents()) >= count, (
        f"only {len(manager.incidents())} incidents by {bound}"
    )


def step_until_state(
    e2e: dict[str, Any],
    incident_id: str,
    want: Any,
    bound_offset: int,
) -> Any:
    """Step until the incident reaches `want` (or fail past the bound)."""
    manager = e2e["manager"]
    loop = e2e["loop"]
    clock = e2e["clock"]
    bound = at_offset(bound_offset)
    record = manager.get(incident_id)
    while record.state != want and clock.now() < bound:
        loop.step()
        record = manager.get(incident_id)
    assert record.state == want, (
        f"{incident_id} is {record.state}, want {want} by {bound}"
    )
    return record
