"""D5 acceptance helpers: tripped worlds, toolsets, scripted turns."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)

READ_TOOLS = {
    "query_logs",
    "get_resource",
    "list_connector_logs",
    "list_job_logs",
    "test_integration_instance",
    "compare_siblings",
    "tripwire_history",
    "get_contract",
}
WORKING_TOOLS = {
    "set_hypotheses",
    "update_plan",
    "record_evidence",
    "submit_diagnosis",
}


def contract(service: str) -> Any:
    from fleetsre.contracts import load_contract

    return load_contract(ROOT / "contracts" / f"{service}.json")


def fleet() -> Any:
    from fleetsre.mock.fleet import load_fleet

    return load_fleet(ROOT / "fixtures" / "fleet.yaml")


def tripwire_of(service: str, tripwire_id: str) -> Any:
    for tripwire in contract(service).tripwires:
        if tripwire.id == tripwire_id:
            return tripwire
    raise AssertionError(f"unknown tripwire {service}/{tripwire_id}")


def connector_name(flt: Any, env: str) -> str:
    for integ, spec in flt.integrations.items():
        if isinstance(spec, dict) and "connectors" in spec:
            key = next(iter(spec["connectors"]))
            inst = flt.environments[env].deployments[
                "alert-ingestion"
            ].connectorInstance
            return (
                f"{flt.instance}/integrations/{integ}/connectors/"
                f"{key}/connectorInstances/{inst}"
            )
    raise AssertionError("no connector integration")


def job_name(flt: Any, env: str) -> str:
    for integ, spec in flt.integrations.items():
        if isinstance(spec, dict) and "jobs" in spec:
            key = next(iter(spec["jobs"]))
            inst = flt.environments[env].deployments[
                "intel-sync"
            ].jobInstance
            return (
                f"{flt.instance}/integrations/{integ}/jobs/"
                f"{key}/jobInstances/{inst}"
            )
    raise AssertionError("no job integration")


def sentinel_integration_name(flt: Any, env: str) -> str:
    for integ, spec in flt.integrations.items():
        if isinstance(spec, dict) and "connectors" in spec:
            return (
                f"{flt.instance}/integrations/{integ}/"
                f"integrationInstances/{env}"
            )
    raise AssertionError("no connector integration")


def trip_until(
    seed: int,
    faults: list[dict[str, Any]],
    service: str,
    deployment: str,
    tripwire_id: str,
    *,
    tick_seconds: int = 15,
) -> dict[str, Any]:
    """Build World+Evaluator, step to the target trip, freeze the clock."""
    from fleetsre.clock import SimClock
    from fleetsre.evaluator.engine import Evaluator
    from fleetsre.mock.world import World
    from fleetsre.ports import MemorySink
    from fleetsre.registry import deployments_from_fleet

    flt = fleet()
    cont = contract(service)
    clock = SimClock(T0)
    sink = MemorySink()
    world = World(flt, seed, clock, sink)
    world.schedule(faults)
    deployments = deployments_from_fleet(flt, cont)
    evaluator = Evaluator([cont], deployments, clock)
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
                and event.tripwire == tripwire_id
            ):
                clock.set(event.at)
                return {
                    "world": world, "clock": clock, "sink": sink,
                    "evaluator": evaluator, "trip": event,
                    "deployments": deployments, "contract": cont,
                    "fleet": flt,
                }
        elapsed += tick_seconds
    raise AssertionError(f"no trip for {deployment} {tripwire_id}")


def setup_from_fixture(fault: str) -> dict[str, Any]:
    """Rebuild the scripted fixture's world and freeze it at the trip."""
    path = ROOT / "fixtures" / "scripted" / f"{fault}.json"
    fixture = json.loads(path.read_text())
    deployment = fixture["incident"]["deployment"]
    service = deployment.split("/", 1)[0]
    setup = trip_until(
        fixture["seed"], fixture["faults"], service,
        deployment, fixture["incident"]["tripwire"],
    )
    setup["fixture"] = fixture
    return setup


def read_prompt() -> str:
    path = ROOT / "prompts" / "investigate.md"
    if not path.is_file():
        raise FileNotFoundError(f"missing SRE prompt: {path}")
    return path.read_text()


def build_registry(
    contracts: list[Any], deployments: list[Any],
    scorecards: Any = None,
) -> Any:
    from fleetsre.registry import Registry

    return Registry(contracts, deployments, scorecards)


def build_tools(
    setup: dict[str, Any], *, verification: bool = True
) -> tuple[Any, Any, dict[str, Any]]:
    """ToolSet over MemoryLogQuery and ChronicleHttpClient(obs-reader)."""
    from fastapi.testclient import TestClient

    from fleetsre.adapters.chronicle_http import ChronicleHttpClient
    from fleetsre.mock.chronicle_api import create_app
    from fleetsre.ports import (
        MemoryLogQuery,
        MemoryStateStore,
    )
    from fleetsre.sre.tools import ToolSet

    sink = setup["sink"]
    world = setup["world"]
    clock = setup["clock"]
    log_query = MemoryLogQuery(sink)
    http = TestClient(create_app(world, dev_auth=True))
    chronicle = ChronicleHttpClient(http, "obs-reader")
    state = MemoryStateStore()
    registry = build_registry(
        [setup["contract"]], setup["deployments"]
    )
    tools = ToolSet(
        log_query, chronicle, state, registry,
        clock=clock, verification=verification,
    )
    parts = {
        "log_query": log_query, "chronicle": chronicle,
        "state": state, "registry": registry,
    }
    return tools, registry, parts


def build_incident(setup: dict[str, Any], registry: Any) -> Any:
    from fleetsre.sre.schemas import Incident

    return Incident.from_trip(
        setup["trip"], registry, setup["clock"].now()
    )


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


def live_gateway_config() -> tuple[Any, str, bool]:
    """Live role from env; prices from FLEETSRE_PRICES_JSON else zero.

    Returns (config, model, prices_zero).
    """
    import os

    from fleetsre.sre.gateway import GatewayConfig, RoleConfig

    model = os.environ.get(
        "FLEETSRE_INVESTIGATE_MODEL", "muse-spark-1.3"
    )
    prices: dict[str, dict[str, float]] = {}
    zero = True
    raw = os.environ.get("FLEETSRE_PRICES_JSON", "")
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            if model in data and isinstance(data[model], dict):
                prices = {model: dict(data[model])}
                zero = False
            elif "input" in data and "output" in data:
                prices = {model: dict(data)}
                zero = False
    if zero:
        prices = {model: {
            "input": 0.0, "cached_input": 0.0, "output": 0.0,
        }}
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
        zero,
    )


def fn_call(
    name: str, args: dict[str, Any], call_id: str
) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args),
    }


def reasoning(n: int) -> dict[str, Any]:
    return {
        "type": "reasoning",
        "id": f"rs_{n:03d}",
        "encrypted_content": f"enc-placeholder-{n}",
    }


def usage(
    turn: int = 1, input_tokens: int = 1000, output_tokens: int = 200,
    cached: int = 600, reasoning_tokens: int = 80,
) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens + turn,
        "output_tokens": output_tokens,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
    }


def scripted_response(
    turn: int, name: str, args: dict[str, Any], **use: Any
) -> dict[str, Any]:
    return {
        "output": [reasoning(turn), fn_call(name, args, f"call_{turn:03d}")],
        "usage": usage(turn, **use),
    }


def ok(envelope: dict[str, Any]) -> dict[str, Any]:
    """Unwrap an accepted ToolSet.call envelope {"callId", "result"}."""
    assert "callId" in envelope, envelope
    result = envelope["result"]
    assert "error" not in result, result
    return result


def refused(envelope: dict[str, Any], rule: str) -> dict[str, Any]:
    """Unwrap a refused envelope; some problem starts with f"{rule}:"."""
    assert "callId" in envelope, envelope
    result = envelope["result"]
    assert "error" in result, result
    problems = result.get("problems", [])
    assert any(
        isinstance(p, str) and p.startswith(f"{rule}:")
        for p in problems
    ), (rule, problems)
    return result


class CountingLogQuery:
    """LogQuery wrapper counting entries/get calls for stuck/crash tests."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.entries_calls = 0
        self.get_calls = 0

    def entries(self, start: Any, end: Any) -> Any:
        self.entries_calls += 1
        return self.inner.entries(start, end)

    def get(self, insert_id: str) -> Any:
        self.get_calls += 1
        return self.inner.get(insert_id)


def valid_hypotheses() -> list[dict[str, Any]]:
    return [
        {"id": "H1", "statement": "Tenant fault on this deployment.",
         "wouldConfirm": "incident signals point here",
         "alternative": False},
        {"id": "H2", "statement": "Fleet-wide upstream outage.",
         "wouldConfirm": "siblings fail the same way",
         "alternative": True},
    ]


def sibling_of(setup: dict[str, Any], deployment: str) -> str:
    service = deployment.split("/", 1)[0]
    env = deployment.split("/", 1)[1]
    envs = sorted(
        d.environment for d in setup["deployments"]
        if d.environment != env
    )
    return f"{service}/{envs[0]}"


def entries_for(setup: dict[str, Any], env: str) -> list[Any]:
    """Sink entries that route to <service>/<env>, as the gates route them."""
    registry = build_registry([setup["contract"]], setup["deployments"])
    want = f"{setup['contract'].service}/{env}"
    return [
        e for e in setup["sink"].entries
        if any(dep == want for dep, _component in registry.route(e))
    ]
