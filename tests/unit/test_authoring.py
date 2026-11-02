"""D6b unit tests: authoring behaviour no acceptance row reaches (d6b-pins 19)."""

import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
D6B = ROOT / "fixtures" / "scripted" / "d6b"
MODEL = "test-author"

_CACHE: dict[str, Any] = {}


def _load_response(name: str) -> dict[str, Any]:
    return json.loads((D6B / f"{name}.json").read_text())


def _gateway(
    responses: list[dict[str, Any]],
    *,
    timeout_s: float = 600,
    retry_timeouts: bool = True,
    max_retries: int = 5,
    sleep: Any = None,
) -> tuple[Any, Any]:
    from fleetsre.sre.gateway import (
        GatewayConfig,
        ModelGateway,
        RoleConfig,
        ScriptedTransport,
    )

    transport = ScriptedTransport(
        [copy.deepcopy(item) for item in responses]
    )
    config = GatewayConfig(
        roles={"author": RoleConfig(
            model=MODEL, reasoning_effort="medium",
            timeout_s=timeout_s, retry_timeouts=retry_timeouts,
        )},
        base_url="https://api.meta.ai/v1",
        max_retries=max_retries,
        prices={MODEL: {
            "input": 4.00, "cached_input": 0.20, "output": 20.00,
        }},
    )
    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return ModelGateway(config, transport, **kwargs), transport


def _brief_samples_scenarios() -> tuple[Any, list[Any], list[Any]]:
    from fleetsre.authoring.brief import load_brief
    from fleetsre.harness.scenario import load_scenario
    from fleetsre.logmodel import read_jsonl

    brief = load_brief(ROOT / "fixtures/briefs/alert-ingestion.yaml")
    samples: list[Any] = []
    for rel in brief.sample_files:
        samples.extend(read_jsonl(ROOT / rel))
    scenarios = [
        load_scenario(p)
        for p in sorted(
            (ROOT / "fixtures/scenarios/alert-ingestion").glob("*.yaml")
        )
    ]
    return brief, samples, scenarios


def _cached_scorer(contract: Any, scenarios: list[Any]) -> Any:
    from fleetsre.harness.scorecard import scorecard

    key = json.dumps(
        contract.model_dump(mode="json"),
        sort_keys=True, separators=(",", ":"),
    )
    if key not in _CACHE:
        _CACHE[key] = scorecard(contract, scenarios)
    return _CACHE[key]


def _usage_numbers(usage: dict[str, Any]) -> dict[str, int]:
    details_in = usage.get("input_tokens_details") or {}
    details_out = usage.get("output_tokens_details") or {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cached_tokens": int(details_in.get("cached_tokens", 0)),
        "reasoning_tokens": int(details_out.get("reasoning_tokens", 0)),
    }


def _expected_cost(numbers: dict[str, int]) -> float:
    return (
        (numbers["input_tokens"] - numbers["cached_tokens"]) * 4.00
        + numbers["cached_tokens"] * 0.20
        + numbers["output_tokens"] * 20.00
    ) / 1_000_000.0


def test_feedback_caps_lists_at_ten_with_tail() -> None:
    """Pin 10: each list shows its first 10 items, then `, and {k} more`."""
    from fleetsre.authoring.loop import (
        AuthoringVersion,
        render_feedback,
    )
    from fleetsre.evaluator.engine import TripEvent
    from fleetsre.harness.replay import T0, ScenarioResult
    from fleetsre.harness.scenario import (
        Expected,
        ExpectedTrip,
        Scenario,
    )
    from fleetsre.harness.scorecard import Scorecard

    scenario = Scenario(
        id="cap", service="alert-ingestion", seed=1,
        durationSeconds=60, deployments=["alpha"], faults=[],
        expected=Expected(trips=[
            ExpectedTrip(
                deployment="alpha", tripwire="dead-connector",
                withinSeconds=600,
            ),
        ]),
        holdout=False,
    )
    missing = [("trip", "alert-ingestion/alpha", "dead-connector")] * 12
    unexpected = [
        TripEvent(
            "trip", "alert-ingestion/alpha", "canary",
            T0 + timedelta(seconds=30 * index), "reason", (),
        )
        for index in range(11)
    ]
    version = AuthoringVersion(
        index=2,
        status="failed",
        scorecard=Scorecard(
            results=[ScenarioResult(
                scenario_id="cap", expected=scenario.expected,
                missing=missing, unexpected=unexpected, passed=False,
            )],
            passed=False,
        ),
    )
    lines = render_feedback(version, [scenario]).split("\n")
    assert lines[0] == (
        "Version 2 failed the scorecard. "
        "0 of 1 visible scenarios passed."
    )
    missing_item = (
        "trip dead-connector on alert-ingestion/alpha "
        "within 600 s of the fault"
    )
    first_unexpected = [
        f"trip canary on alert-ingestion/alpha at +{30 * index} s"
        for index in range(10)
    ]
    assert lines[1] == (
        "cap: missing "
        + ", ".join([missing_item] * 10)
        + ", and 2 more; unexpected "
        + ", ".join(first_unexpected)
        + ", and 1 more"
    )


def test_flatten_schema_inlines_definitions_and_drops_meta() -> None:
    """Pin 3: `#/definitions/…` inlines; meta keys drop."""
    from fleetsre.authoring.schema import flatten_schema

    flat = flatten_schema({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.test/contract",
        "$comment": "meta",
        "definitions": {"name": {"type": "string"}},
        "properties": {"x": {"$ref": "#/definitions/name"}},
    })
    assert flat == {"properties": {"x": {"type": "string"}}}


def test_flatten_schema_raises_on_recursive_ref() -> None:
    """Pin 3: a recursive `$ref` raises ValueError."""
    from fleetsre.authoring.schema import flatten_schema

    with pytest.raises(ValueError, match="recursive"):
        flatten_schema({
            "$defs": {"a": {"$ref": "#/$defs/a"}},
            "properties": {"x": {"$ref": "#/$defs/a"}},
        })
    with pytest.raises(ValueError, match="recursive"):
        flatten_schema({
            "$defs": {
                "a": {"$ref": "#/$defs/b"},
                "b": {"$ref": "#/$defs/a"},
            },
            "properties": {"x": {"$ref": "#/$defs/a"}},
        })


def test_no_retry_on_timeout_when_disabled() -> None:
    """Pin 4: with retry_timeouts=False a timeout is raised at once."""
    from fleetsre.sre.gateway import TransportError

    slept: list[float] = []
    gateway, transport = _gateway(
        [{"_error": 504, "_timeout": True}], sleep=slept.append,
        retry_timeouts=False,
    )
    with pytest.raises(TransportError) as excinfo:
        gateway.respond(
            "author", [{"role": "user", "content": "hi"}], None,
            text_format={"type": "json_schema"},
        )
    assert excinfo.value.timeout is True
    assert len(transport.requests) == 1
    assert transport.timeouts == [600]
    assert slept == []


def test_retry_timeouts_false_still_retries_429() -> None:
    """Pin 4: 429 still retries when timeout retries are off."""
    slept: list[float] = []
    gateway, transport = _gateway(
        [{"_error": 429}, {"output": [], "usage": {}}],
        sleep=slept.append, retry_timeouts=False,
    )
    response = gateway.respond(
        "author", [{"role": "user", "content": "hi"}], None,
        text_format={"type": "json_schema"},
    )
    assert response.output == []
    assert len(transport.requests) == 2
    assert slept == [1.0]


def test_model_unavailable_keeps_versions() -> None:
    """Pin 12: giving up keeps the versions already made."""
    from fleetsre.authoring.loop import author_contract

    brief, samples, scenarios = _brief_samples_scenarios()
    gateway, transport = _gateway(
        [_load_response("fail-visible"), {"_error": 500}],
        max_retries=0,
    )
    result = author_contract(
        brief, samples, scenarios, gateway,
        max_revisions=3, scorer=_cached_scorer,
    )
    assert result.passed is False
    assert result.stop_reason == "model_unavailable"
    assert len(result.versions) == 1
    assert result.versions[0].status == "failed"
    assert result.versions[0].scorecard is not None
    assert len(result.scorecards) == 1
    assert result.contract is not None
    assert len(transport.requests) == 2


def _fenced_response(text: str, fence: str | None) -> dict[str, Any]:
    if fence is not None:
        text = f"```{fence}\n{text}\n```"
    return {
        "output": [{
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }],
        "usage": {
            "input_tokens": 100, "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
    }


def test_code_fence_stripped() -> None:
    """Pin 9: one surrounding fence, with or without `json`, strips."""
    from fleetsre.authoring.loop import author_contract

    brief, samples, scenarios = _brief_samples_scenarios()
    text = _load_response("pass")["output"][0]["content"][0]["text"]
    for fence in ("json", ""):
        gateway, _transport = _gateway([_fenced_response(text, fence)])
        result = author_contract(
            brief, samples, scenarios, gateway,
            max_revisions=0, scorer=_cached_scorer,
        )
        assert result.versions[0].status == "passed"
        assert result.passed is True


def test_non_object_json_problem() -> None:
    """Pin 9: valid JSON that is not an object is `json: not an object`."""
    from fleetsre.authoring.loop import author_contract, render_feedback

    brief, samples, scenarios = _brief_samples_scenarios()
    gateway, _transport = _gateway([
        _fenced_response("[1, 2, 3]", None),
        _load_response("pass"),
    ])
    result = author_contract(
        brief, samples, scenarios, gateway,
        max_revisions=3, scorer=_cached_scorer,
    )
    first = result.versions[0]
    assert first.status == "invalid_json"
    assert first.problems == ["json: not an object"]
    assert first.contract is None
    assert first.feedback == (
        "Version 1 is not valid JSON: not an object. "
        "Return the whole contract as one JSON object."
    )
    assert first.feedback == render_feedback(first, scenarios)
    assert result.versions[1].status == "passed"


def test_per_version_cost_and_summed_tokens() -> None:
    """Pin 13: usage and cost per version; tokens and cost summed."""
    from fleetsre.authoring.loop import author_contract

    brief, samples, scenarios = _brief_samples_scenarios()
    fail = _load_response("fail-visible")
    passed = _load_response("pass")
    gateway, _transport = _gateway([fail, passed])
    result = author_contract(
        brief, samples, scenarios, gateway,
        max_revisions=3, scorer=_cached_scorer,
    )
    assert [v.status for v in result.versions] == ["failed", "passed"]
    totals = {
        "input_tokens": 0, "output_tokens": 0,
        "cached_tokens": 0, "reasoning_tokens": 0,
    }
    for version, response in zip(result.versions, [fail, passed]):
        numbers = _usage_numbers(response["usage"])
        assert version.usage == numbers
        assert version.cost_usd == pytest.approx(
            _expected_cost(numbers)
        )
        for key in totals:
            totals[key] += numbers[key]
    assert result.tokens == totals
    assert result.cost_usd == pytest.approx(sum(
        _expected_cost(_usage_numbers(response["usage"]))
        for response in (fail, passed)
    ))


def test_gateway_tolerates_transport_without_timeout() -> None:
    """Doubles written against the D5 interface keep working."""

    class LegacyTransport:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        def create(self, request: dict[str, Any]) -> dict[str, Any]:
            self.requests.append(request)
            return {"output": [], "usage": {}}

    from fleetsre.sre.gateway import (
        GatewayConfig,
        ModelGateway,
        RoleConfig,
    )

    legacy = LegacyTransport()
    gateway = ModelGateway(
        GatewayConfig(
            roles={"investigate": RoleConfig(
                model="m", reasoning_effort="low",
            )},
            base_url="https://x",
        ),
        legacy,
    )
    response = gateway.respond(
        "investigate", [{"role": "user", "content": "hi"}],
        [{"type": "function", "name": "t"}],
    )
    assert response.output == []
    assert len(legacy.requests) == 1
    assert legacy.requests[0]["parallel_tool_calls"] is False


def test_on_version_called_in_order_with_final_feedback() -> None:
    """on_version fires once per version; each has its final feedback."""
    from fleetsre.authoring.loop import author_contract

    brief, samples, scenarios = _brief_samples_scenarios()
    gateway, _transport = _gateway([
        _load_response("fail-visible"), _load_response("pass"),
    ])
    seen: list[Any] = []
    result = author_contract(
        brief, samples, scenarios, gateway,
        max_revisions=3, scorer=_cached_scorer, on_version=seen.append,
    )
    assert [v.index for v in seen] == [1, 2]
    assert seen == result.versions
    assert seen[0].feedback is not None
    assert seen[0].feedback == result.versions[0].feedback
    assert seen[1].feedback is None


def test_cli_writes_out_file_after_each_run(tmp_path: Any) -> None:
    """The --out file holds completed runs even if a later run crashes."""
    from fleetsre.authoring.__main__ import main

    brief = str(ROOT / "fixtures/briefs/alert-ingestion.yaml")
    script = str(D6B / "demo.json")
    out = tmp_path / "out.json"
    rc = main([
        "--brief", brief, "--script", script,
        "--runs", "2", "--out", str(out),
    ])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert [run["run"] for run in payload["runs"]] == [1, 2]


def test_cli_keeps_run_1_when_run_2_fails(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A crash in run 2 leaves a valid file with run 1 in it."""
    import fleetsre.authoring.loop as loop
    from fleetsre.authoring.__main__ import main

    real = loop.author_contract
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom in run 2")
        return real(*args, **kwargs)

    monkeypatch.setattr(
        "fleetsre.authoring.loop.author_contract", flaky
    )
    out = tmp_path / "out.json"
    with pytest.raises(RuntimeError, match="boom in run 2"):
        main([
            "--brief", str(ROOT / "fixtures/briefs/alert-ingestion.yaml"),
            "--script", str(D6B / "demo.json"),
            "--runs", "2", "--out", str(out),
        ])
    payload = json.loads(out.read_text())
    assert [run["run"] for run in payload["runs"]] == [1]
    assert payload["runs"][0]["passed"] is True
