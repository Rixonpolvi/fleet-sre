"""Single-act demo runtime (d8-pins 4). A replica of one act's D6 E2E world."""

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _service_for_incident(incident_id: str) -> str:
    for service in ("alert-ingestion", "intel-sync"):
        if incident_id.startswith(f"inc-{service}-"):
            return service
    return "alert-ingestion"


def _load_responses(paths: list[str]) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    for rel in paths:
        candidate = Path(rel)
        if not candidate.is_file():
            candidate = ROOT / rel
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        responses.extend(payload["responses"])
    return responses


def _scripted_config() -> Any:
    from fleetsre.sre.gateway import GatewayConfig, RoleConfig

    model = "demo-scripted"
    return GatewayConfig(
        roles={"investigate": RoleConfig(
            model=model, reasoning_effort="medium"
        )},
        base_url="https://api.meta.ai/v1",
        max_retries=5,
        prices={model: {
            "input": 0.0, "cached_input": 0.0, "output": 0.0,
        }},
    )


def _live_config() -> Any:
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
    return GatewayConfig(
        roles={"investigate": RoleConfig(
            model=model, reasoning_effort="medium"
        )},
        base_url="https://api.meta.ai/v1",
        max_retries=5,
        prices=prices,
    )


class _CountingTransport:
    """Counts model calls and stamps each with the sim time (d8-pins 4)."""

    def __init__(
        self,
        inner: Any,
        clock: Any,
        lock: threading.RLock,
        *,
        activity: Any = None,
    ) -> None:
        self._inner = inner
        self._clock = clock
        self._lock = lock
        self._activity = activity
        self.call_times: list[datetime] = []
        self.requests: list[dict[str, Any]] = []

    def create(
        self, request: dict[str, Any], *, timeout: float = 120.0
    ) -> dict[str, Any]:
        with self._lock:
            self.call_times.append(self._clock.now())
            self.requests.append(json.loads(json.dumps(
                request, default=str
            )))
        if self._activity is not None:
            self._activity.set_call_in_flight(True)
        try:
            return self._inner.create(request, timeout=timeout)
        finally:
            if self._activity is not None:
                self._activity.set_call_in_flight(False)


class DemoRuntime:
    """A replica of one act's D6 end-to-end world (d8-pins 4).

    Members: act, clock, world, sink, evaluator, registry, manager,
    store, loop, transport (DemoTransport, or None when live), lock,
    started_at, held, model_calls, last_model_call_at, activity.
    """

    def __init__(
        self,
        act: Any,
        *,
        live: bool = False,
        speed: float = 10.0,
        lead_s: int = 60,
        turn_delay_s: float = 0.0,
    ) -> None:
        from fastapi.testclient import TestClient

        from fleetsre.adapters.chronicle_http import ChronicleHttpClient
        from fleetsre.clock import SimClock
        from fleetsre.contracts import load_contract
        from fleetsre.control.approvals import ApprovalStore
        from fleetsre.control.executor import Executor
        from fleetsre.control.incidents import IncidentManager
        from fleetsre.control.journal import Journal
        from fleetsre.control.loop import ControlLoop
        from fleetsre.control.verify import Verifier
        from fleetsre.demo.activity import (
            Activity,
            ActivityInvestigator,
            ActivitySessionStore,
        )
        from fleetsre.demo.transport import DemoTransport
        from fleetsre.evaluator.canary import CanaryScheduler
        from fleetsre.evaluator.engine import Evaluator
        from fleetsre.mock.chronicle_api import create_app
        from fleetsre.mock.fleet import load_fleet
        from fleetsre.mock.world import World
        from fleetsre.ports import (
            MemoryLogQuery,
            MemorySink,
            MemoryStateStore,
        )
        from fleetsre.registry import Registry, deployments_from_fleet
        from fleetsre.sre.gateway import ModelGateway, OpenAITransport
        from fleetsre.sre.investigator import Investigator
        from fleetsre.sre.session import MemorySessionStore
        from fleetsre.sre.tools import ToolSet

        self.act = act
        self.live = live
        self.speed = speed
        self.lead_s = lead_s
        self.turn_delay_s = float(turn_delay_s)
        self.act_name = ""
        self.lock: threading.RLock = threading.RLock()
        self.activity = Activity()
        t0: datetime = act.t0

        service = _service_for_incident(act.incident)
        fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
        contract = load_contract(ROOT / "contracts" / f"{service}.json")
        clock = SimClock(t0)
        sink = MemorySink()
        world = World(fleet, act.seed, clock, sink)
        world.schedule([fault.schedule_item() for fault in act.faults])
        deployments = deployments_from_fleet(fleet, contract)
        evaluator = Evaluator([contract], deployments, clock)
        scheduler = CanaryScheduler(world, evaluator, [contract], clock)

        if live:
            inner: Any = OpenAITransport(
                api_key=os.environ.get("MODEL_API_KEY", ""),
                base_url="https://api.meta.ai/v1",
            )
            config = _live_config()
            transport: Any = None
            script_length = 0
        else:
            responses = _load_responses(list(act.responses))
            transport = DemoTransport(
                responses, turn_delay_s=self.turn_delay_s
            )
            inner = transport
            config = _scripted_config()
            script_length = len(responses)
        counting = _CountingTransport(
            inner, clock, self.lock, activity=self.activity
        )
        gateway = ModelGateway(config, counting)

        log_query = MemoryLogQuery(sink)
        http: Any = TestClient(create_app(world, dev_auth=True))
        obs: Any = ChronicleHttpClient(http, "obs-reader")
        deployer: Any = ChronicleHttpClient(http, "deployer")
        reader: Any = ChronicleHttpClient(http, "obs-reader")
        state = MemoryStateStore()
        registry = Registry([contract], deployments)
        tools = ToolSet(
            log_query, obs, state, registry, clock=clock,
            verification=True,
        )
        store = ActivitySessionStore(
            MemorySessionStore(), self.activity
        )
        inner_investigator = Investigator(tools, gateway, store)
        investigator = ActivityInvestigator(
            inner_investigator, self.activity,
            str(config.roles["investigate"].model),
        )
        approvals = ApprovalStore(clock)
        journal = Journal()
        executor = Executor(deployer, approvals, journal, clock=clock)
        verifier = Verifier(evaluator, registry, clock)
        manager = IncidentManager(
            evaluator, investigator, approvals, executor, verifier,
            state, clock, registry=registry, reader=reader,
        )
        investigator.set_manager(manager)
        loop = ControlLoop(
            world, evaluator, scheduler, manager, sink, t0=t0,
            tick_seconds=act.tick_seconds,
        )

        self.clock = clock
        self.world = world
        self.sink = sink
        self.evaluator = evaluator
        self.registry = registry
        self.manager = manager
        self.store = store
        self.loop = loop
        self.transport = transport
        self.gateway = gateway
        self._counting = counting
        self.script_length = script_length
        self.started_at: datetime = t0

    @property
    def model_calls(self) -> int:
        with self.lock:
            return len(self._counting.call_times)

    @property
    def last_model_call_at(self) -> datetime | None:
        with self.lock:
            if not self._counting.call_times:
                return None
            return self._counting.call_times[-1]

    @property
    def call_times(self) -> list[datetime]:
        with self.lock:
            return list(self._counting.call_times)

    @property
    def held(self) -> bool:
        with self.lock:
            return self._is_held()

    def _is_held(self) -> bool:
        from fleetsre.control.incidents import IncidentState

        for record in self.manager.incidents():
            if record.id == self.act.incident:
                return record.state == IncidentState.AWAITING_APPROVAL
        return False

    def start(self) -> None:
        """Step until first_fault_at - lead_s; record started_at."""
        with self.lock:
            first_at = self.act.t0 + timedelta(
                seconds=min(fault.at for fault in self.act.faults)
            )
            target = first_at - timedelta(seconds=self.lead_s)
            while self.clock.now() < target:
                self.loop.step()
            self.started_at = self.clock.now()

    def advance(self, seconds: float) -> None:
        """Step until the clock has moved `seconds` (d8-pins 4, 9)."""
        with self.lock:
            target = self.clock.now() + timedelta(seconds=seconds)
            while self.clock.now() < target:
                if self._is_held():
                    break
                self.loop.step()

    def run_realtime(self, stop: Event) -> None:
        """Advance one tick every tick_seconds / speed real seconds."""
        interval = self.act.tick_seconds / self.speed if self.speed else 1.0
        while not stop.is_set():
            if stop.wait(interval):
                break
            self.advance(self.act.tick_seconds)

    def active_faults(self) -> list[dict[str, Any]]:
        """The world's active faults: id, type, environment."""
        with self.lock:
            return [
                {
                    "id": fault["id"],
                    "type": fault["type"],
                    "environment": fault["environment"],
                }
                for fault in self.world.list_faults()
            ]

    def clear_fault(self, fault_id: str, by: str = "operator") -> None:
        """Clear the fault and append the demo timeline event (d8-pins 4)."""
        from fleetsre.control.incidents import IncidentState
        from fleetsre.control.postmortem import TimelineEvent

        with self.lock:
            known = {
                fault["id"]: fault
                for fault in self.world.list_faults()
            }
            if fault_id not in known:
                self.world.clear(fault_id)
                return
            environment = known[fault_id]["environment"]
            self.world.clear(fault_id)
            closed = frozenset({
                IncidentState.RESOLVED, IncidentState.ESCALATED,
            })
            for record in self.manager.incidents():
                deployment = record.incident.deployment
                env = deployment.split("/", 1)[-1]
                if record.state in closed or env != environment:
                    continue
                record.timeline.append(TimelineEvent(
                    at=self.clock.now(), kind="demo",
                    detail={
                        "action": "mark_rotated", "fault": fault_id,
                        "by": by,
                    },
                ))
