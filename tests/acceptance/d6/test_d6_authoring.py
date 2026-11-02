"""D6 acceptance: contract authoring loop (d6b-pins 16-17).

Setup per pin 16: the alert-ingestion brief plus its sample files, every
alert-ingestion scenario, ScriptedTransport with the role author configured,
and a module-scoped caching wrapper around the real scorecard. Response
sequences come from the single-response files in fixtures/scripted/d6b/.

Pre-flighted outcomes (P4, real scorer, same scenarios in sorted order):
fail-visible (lazy-canary without _mutation) fails the visible green-liar
and stall plus the recovery holdout; fail-holdout (connector-errors
threshold 8 -> 3) passes every visible scenario and fails flap-mild only;
pass (the reference contract) passes everything.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
D6B = ROOT / "fixtures/scripted/d6b"
BRIEF_PATH = ROOT / "fixtures/briefs/alert-ingestion.yaml"
SCEN_DIR = ROOT / "fixtures/scenarios/alert-ingestion"

AUTHOR_MODEL = "test-author"
AUTHOR_TIMEOUT = 600

_SCORECARD_CACHE: dict[str, Any] = {}


def _load_response(name: str) -> dict[str, Any]:
    path = D6B / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing D6b scripted fixture: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or "output" not in data:
        raise AssertionError(f"{path} is not a single response")
    return data


def _author_gateway(
    responses: list[dict[str, Any]],
) -> tuple[Any, Any]:
    """ModelGateway with the author role on a ScriptedTransport."""
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
            model=AUTHOR_MODEL, reasoning_effort="medium",
            timeout_s=AUTHOR_TIMEOUT,
        )},
        base_url="https://api.meta.ai/v1",
        max_retries=5,
        prices={AUTHOR_MODEL: {
            "input": 4.00, "cached_input": 0.20, "output": 20.00,
        }},
    )
    return ModelGateway(config, transport), transport


def _brief_samples_scenarios() -> tuple[Any, list[Any], list[Any]]:
    """Brief, samples and scenarios per d6b-pins 16."""
    from fleetsre.authoring.brief import load_brief
    from fleetsre.harness.scenario import load_scenario
    from fleetsre.logmodel import read_jsonl

    brief = load_brief(BRIEF_PATH)
    samples: list[Any] = []
    for rel in brief.sample_files:
        samples.extend(read_jsonl(ROOT / rel))
    scenarios = [load_scenario(p) for p in sorted(SCEN_DIR.glob("*.yaml"))]
    assert samples and scenarios
    return brief, samples, scenarios


def _cached_scorer(contract: Any, scenarios: list[Any]) -> Any:
    """Real scorecard cached by the contract's canonical JSON.

    The key also carries the scenario IDs: T21 renames the holdouts, and
    the renamed run must score fresh rather than reuse the cached result.
    Never a fabricated Scorecard (d6b-pins 5).
    """
    from fleetsre.harness.scorecard import scorecard

    canonical = json.dumps(
        contract.model_dump(mode="json"),
        sort_keys=True, separators=(",", ":"),
    )
    key = canonical + "|" + "|".join(s.id for s in scenarios)
    if key not in _SCORECARD_CACHE:
        _SCORECARD_CACHE[key] = scorecard(contract, scenarios)
    return _SCORECARD_CACHE[key]


def _rename_holdouts(scenarios: list[Any]) -> list[Any]:
    """Give every holdout a distinctive ID (d6b-pins 17, T21)."""
    return [
        s.model_copy(update={"id": f"hz-{s.id}-q7"})
        if s.holdout else s
        for s in scenarios
    ]


def _within_seconds(
    scenario: Any, kind: str, tripwire: str, deployment_id: str,
) -> int:
    """Deadline of a missing item, read from the scenario's expected."""
    env = deployment_id.split("/", 1)[1]
    items = (
        scenario.expected.trips
        if kind == "trip" else scenario.expected.recoveries
    )
    for item in items:
        if item.deployment == env and item.tripwire == tripwire:
            return item.withinSeconds
    raise AssertionError(
        f"no expected {kind} for {tripwire} on {deployment_id}"
    )


def _render_items(rendered: list[str]) -> str:
    """One missing/unexpected list in the pin 10 feedback format."""
    if not rendered:
        return "none"
    text = ", ".join(rendered[:10])
    if len(rendered) > 10:
        text += f", and {len(rendered) - 10} more"
    return text


def _failed_feedback_lines(
    n: int, vscorecard: Any, scenarios: list[Any],
) -> list[str]:
    """Pin 10 feedback lines for a failed version, rebuilt by the test."""
    from fleetsre.harness.replay import T0

    results = {r.scenario_id: r for r in vscorecard.results}
    visible = [s for s in scenarios if not s.holdout]
    passed = sum(1 for s in visible if results[s.id].passed)
    lines = [(
        f"Version {n} failed the scorecard. "
        f"{passed} of {len(visible)} visible scenarios passed."
    )]
    for scenario in visible:
        result = results[scenario.id]
        if result.passed:
            continue
        missing = []
        for kind, deployment, tripwire in result.missing:
            within = _within_seconds(
                scenario, kind, tripwire, deployment
            )
            if kind == "trip":
                missing.append(
                    f"trip {tripwire} on {deployment} "
                    f"within {within} s of the fault"
                )
            else:
                missing.append(
                    f"recovery {tripwire} on {deployment} "
                    f"within {within} s of the fault clearing"
                )
        unexpected = []
        for event in result.unexpected:
            offset = int((event.at - T0).total_seconds())
            unexpected.append(
                f"{event.kind} {event.tripwire} on {event.deployment} "
                f"at +{offset} s"
            )
        lines.append(
            f"{scenario.id}: missing {_render_items(missing)}; "
            f"unexpected {_render_items(unexpected)}"
        )
    return lines


def _user_texts(request: dict[str, Any]) -> list[str]:
    """Text of the developer and user input items of a model request."""
    texts = []
    for item in request.get("input", []):
        if not isinstance(item, dict):
            continue
        if item.get("role") not in ("developer", "user"):
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.append(" ".join(
                part.get("text", "") for part in content
                if isinstance(part, dict)
            ))
        else:
            texts.append(str(content))
    return texts


def _overwrite(brief: Any, data: dict[str, Any], n: int) -> dict[str, Any]:
    """Pin 9's overwrites: service, version and provenance from the brief."""
    out = copy.deepcopy(data)
    out["service"] = brief.service
    out["version"] = brief.version
    out["provenance"] = {
        "authoredBy": AUTHOR_MODEL,
        "scorecard": f"author-v{n}",
        "approvedBy": None,
    }
    return out


def _response_text(response: dict[str, Any]) -> str:
    return str(response["output"][0]["content"][0]["text"])


def test_d6_t11_authoring_loop():
    """D6-T11: v1 fails visibly, v2 passes; a never-passing model stops."""
    import yaml

    from fleetsre.authoring.loop import (
        author_contract,
        render_brief,
        render_feedback,
    )
    from fleetsre.authoring.schema import authoring_schema

    brief, samples, scenarios = _brief_samples_scenarios()
    renamed = _rename_holdouts(scenarios)
    renamed_ids = [s.id for s in renamed if s.holdout]
    fail = _load_response("fail-visible")
    passed = _load_response("pass")
    schema = authoring_schema()
    assert "$ref" not in json.dumps(schema)
    assert "$defs" not in schema
    assert "patternProperties" not in schema
    assert "provenance" not in schema["properties"]
    assert "provenance" not in schema.get("required", [])

    gateway, transport = _author_gateway([fail, passed])
    result = author_contract(
        brief, samples, renamed, gateway, scorer=_cached_scorer,
    )
    assert result.passed is True
    assert result.stop_reason == "passed"
    assert len(result.versions) == 2
    assert len(result.scorecards) == 2
    assert [v.status for v in result.versions] == ["failed", "passed"]
    assert len(transport.requests) == 2

    first = transport.requests[0]
    assert first["text"]["format"]["type"] == "json_schema"
    assert first["text"]["format"]["name"] == "contract"
    assert first["text"]["format"]["schema"] == schema
    assert "strict" not in first["text"]["format"]
    assert "tools" not in first
    assert "parallel_tool_calls" not in first
    assert first["model"] == AUTHOR_MODEL
    assert first["store"] is False
    assert transport.timeouts == [AUTHOR_TIMEOUT, AUTHOR_TIMEOUT]
    prompt = (ROOT / "prompts/author.md").read_text()
    assert len(first["input"]) == 2
    assert first["input"][0]["role"] == "developer"
    assert first["input"][0]["content"] == prompt
    assert first["input"][1]["role"] == "user"
    assert first["input"][1]["content"] == render_brief(
        brief, samples, schema, versions=4,
    )
    expected_brief = "\n\n".join([
        "# Service brief\n" + yaml.safe_dump(
            brief.model_dump(exclude={"sample_files"}), sort_keys=False,
        ),
        "# Sample log entries\n" + "\n".join(
            json.dumps(e.model_dump(mode="json"), separators=(",", ":"))
            for e in samples
        ),
        "# Contract JSON schema\n" + json.dumps(schema, indent=2),
        "# Versions\nYou have 4 versions. After each failed version you "
        "get feedback; reply with the whole revised contract.",
    ])
    assert first["input"][1]["content"] == expected_brief

    second = transport.requests[1]
    assert second["text"]["format"]["schema"] == schema
    assert "tools" not in second
    assert second["input"][:2] == first["input"]
    assert second["input"][2:2 + len(fail["output"])] == fail["output"]
    last = second["input"][-1]
    assert last["role"] == "user"
    version1 = result.versions[0]
    assert last["content"] == render_feedback(version1, renamed)
    assert version1.scorecard is not None
    assert last["content"] == "\n".join(
        _failed_feedback_lines(1, version1.scorecard, renamed)
    )
    assert "held-out" not in last["content"]
    for holdout_id in renamed_ids:
        assert holdout_id not in last["content"]

    gateway4, transport4 = _author_gateway([fail] * 4)
    result4 = author_contract(
        brief, samples, scenarios, gateway4,
        max_revisions=3, scorer=_cached_scorer,
    )
    assert result4.passed is False
    assert result4.stop_reason == "max_revisions"
    assert len(result4.versions) == 4
    assert len(result4.scorecards) == 4
    assert len(transport4.requests) == 4

    gateway1, transport1 = _author_gateway([fail] * 2)
    result1 = author_contract(
        brief, samples, scenarios, gateway1,
        max_revisions=1, scorer=_cached_scorer,
    )
    assert len(transport1.requests) == 2
    assert len(result1.versions) == 2
    assert result1.stop_reason == "max_revisions"


def test_d6_t12_authoring_hides_exam():
    """D6-T12: no scenario expected block, fault or schedule reaches a prompt."""
    from fleetsre.authoring.loop import author_contract

    brief, samples, scenarios = _brief_samples_scenarios()
    fail = _load_response("fail-visible")
    passed = _load_response("pass")
    holdout_fail = _load_response("fail-holdout")
    renamed = _rename_holdouts(scenarios)

    runs = [
        (scenarios, [fail, passed]),
        (renamed, [holdout_fail, passed]),
        (renamed, [holdout_fail] * 4),
    ]
    requests: list[dict[str, Any]] = []
    for run_scenarios, sequence in runs:
        gateway, transport = _author_gateway(sequence)
        author_contract(
            brief, samples, run_scenarios, gateway,
            scorer=_cached_scorer,
        )
        requests.extend(transport.requests)
    assert len(requests) == 2 + 2 + 4

    expected_dumps = [
        json.dumps(s.expected.model_dump(mode="json")) for s in scenarios
    ]
    fault_types = {f.type for s in scenarios for f in s.faults}
    assert fault_types, "scenarios carry no fault types to check"
    for request in requests:
        for text in _user_texts(request):
            assert '"expected":' not in text
            assert "\nexpected:" not in text
            assert "mustNotTrip" not in text
            assert "withinSeconds" not in text
            assert '"faults"' not in text
            assert "faults:" not in text
            for dump in expected_dumps:
                assert dump not in text
            for fault_type in fault_types:
                assert fault_type not in text


def test_d6_t13_authored_output_valid():
    """D6-T13: bad JSON and a bad contract fail revisions without scoring."""
    from fleetsre.authoring.loop import author_contract
    from fleetsre.contracts import ContractError, parse_contract

    brief, samples, scenarios = _brief_samples_scenarios()
    bad_json = _load_response("invalid-json")
    bad_contract = _load_response("invalid-contract")
    passed = _load_response("pass")

    calls: list[Any] = []

    def spy(contract: Any, scens: list[Any]) -> Any:
        calls.append(contract)
        return _cached_scorer(contract, scens)

    gateway, transport = _author_gateway([bad_json, bad_contract, passed])
    result = author_contract(
        brief, samples, scenarios, gateway, scorer=spy,
    )
    assert [v.status for v in result.versions] == [
        "invalid_json", "invalid_contract", "passed",
    ]
    assert result.passed is True
    assert result.stop_reason == "passed"
    assert len(result.scorecards) == 1
    assert len(transport.requests) == 3
    assert len(calls) == 1
    overwritten = _overwrite(brief, json.loads(_response_text(passed)), 3)
    assert calls[0] == parse_contract(overwritten)
    assert result.versions[2].contract == overwritten

    bad_text = _response_text(bad_json)
    try:
        json.loads(bad_text)
    except json.JSONDecodeError as exc:
        json_error = str(exc)
    else:
        raise AssertionError("invalid-json fixture parses")
    assert result.versions[0].problems == [f"json: {json_error}"]
    assert result.versions[0].feedback == (
        f"Version 1 is not valid JSON: {json_error}. "
        "Return the whole contract as one JSON object."
    )

    bad_data = json.loads(_response_text(bad_contract))
    try:
        parse_contract(_overwrite(brief, bad_data, 2))
    except ContractError as exc:
        code = exc.code
        message = str(exc)
    else:
        raise AssertionError("invalid-contract fixture validates")
    assert code == "duplicate_tripwire"
    assert result.versions[1].problems == [f"contract.{code}: {message}"]
    assert result.versions[1].feedback == (
        f"Version 2 was rejected by the contract loader: {code}: {message}"
    )
    assert result.versions[2].feedback is None
    assert result.versions[0].contract is None
    assert transport.requests[1]["input"][-1] == {
        "role": "user", "content": result.versions[0].feedback,
    }
    assert transport.requests[2]["input"][-1] == {
        "role": "user", "content": result.versions[1].feedback,
    }


@pytest.mark.live
def test_d6_t15_live_authoring_eval():
    """D6-T15: three live runs against alert-ingestion; at least two pass."""
    import os
    import subprocess
    import sys
    from datetime import datetime, timezone

    if not os.environ.get("MODEL_API_KEY"):
        pytest.skip("MODEL_API_KEY is not set; live eval needs a key")

    date = datetime.now(timezone.utc).date().isoformat()
    out_path = ROOT / "evals" / f"d6-{date}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if "FLEETSRE_PRICES_JSON" not in env:
        env["FLEETSRE_PRICES_JSON"] = json.dumps({
            "muse-wonder-1.0": {
                "input": 4.00, "cached_input": 0.20, "output": 20.00,
            },
        })
    proc = subprocess.run(
        [sys.executable, "-m", "fleetsre.authoring",
         "--brief", str(BRIEF_PATH), "--runs", "3",
         "--out", str(out_path)],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    version_lines = [
        line for line in proc.stdout.splitlines()
        if line.startswith("VERSION ")
    ]
    result_lines = [
        line for line in proc.stdout.splitlines()
        if line.startswith("RESULT ")
    ]
    assert len(result_lines) == 3
    assert len(version_lines) >= 3
    for line in version_lines:
        body = json.loads(line[len("VERSION "):])
        for key in ("run", "version", "status", "visiblePassed",
                    "visibleTotal", "holdoutsPassed", "falsePositives",
                    "problems", "costUsd"):
            assert key in body, body

    text = out_path.read_text()
    assert "encrypted_content" not in text
    payload = json.loads(text)
    runs = payload["runs"]
    assert len(runs) == 3
    passed_runs = 0
    for run, line in zip(runs, result_lines):
        body = json.loads(line[len("RESULT "):])
        for key in ("run", "passed", "stopReason", "versions",
                    "costUsd"):
            assert key in body, body
        assert run["passed"] == body["passed"]
        assert run["stop_reason"] == body["stopReason"]
        if run["passed"]:
            passed_runs += 1
        assert run["versions"], run
        for version in run["versions"]:
            for key in ("status", "problems", "feedback", "usage",
                        "cost_usd"):
                assert key in version, version
            assert version["scenarios"], version
            for entry in version["scenarios"]:
                for key in ("scenario_id", "passed", "missing",
                            "unexpected"):
                    assert key in entry, entry
        assert run["contract"] is not None
        assert run["tokens"]
        assert run["cost_usd"] >= 0
    assert passed_runs >= 2


def test_d6_t21_held_out_scenarios():
    """D6-T21: holdouts stay unnamed; only they can fail a passing run."""
    from fleetsre.authoring.loop import author_contract

    brief, samples, scenarios = _brief_samples_scenarios()
    assert len([s for s in scenarios if s.holdout]) >= 1
    assert len([s for s in scenarios if not s.holdout]) >= 1
    renamed = _rename_holdouts(scenarios)
    renamed_ids = [s.id for s in renamed if s.holdout]
    assert renamed_ids
    assert all(
        rid.startswith("hz-") and rid.endswith("-q7")
        for rid in renamed_ids
    )

    fail = _load_response("fail-holdout")
    passed = _load_response("pass")

    gateway, transport = _author_gateway([fail, passed])
    result = author_contract(
        brief, samples, renamed, gateway, scorer=_cached_scorer,
    )
    assert result.passed is True
    assert result.stop_reason == "passed"
    assert len(result.versions) == 2
    version1 = result.versions[0]
    assert version1.status == "failed"
    assert version1.feedback == (
        "Version 1 failed the scorecard: a held-out scenario failed."
    )
    assert version1.scorecard is not None
    by_id = {r.scenario_id: r for r in version1.scorecard.results}
    assert all(
        by_id[s.id].passed for s in renamed if not s.holdout
    )
    assert any(
        not by_id[s.id].passed for s in renamed if s.holdout
    )

    gateway4, transport4 = _author_gateway([fail] * 4)
    result4 = author_contract(
        brief, samples, renamed, gateway4,
        max_revisions=3, scorer=_cached_scorer,
    )
    assert result4.passed is False
    assert result4.stop_reason == "max_revisions"
    assert len(result4.versions) == 4
    for version in result4.versions:
        assert version.status == "failed"
        assert version.scorecard is not None
        scored = {r.scenario_id: r for r in version.scorecard.results}
        assert all(
            scored[s.id].passed for s in renamed if not s.holdout
        )

    for request in list(transport.requests) + list(transport4.requests):
        for text in _user_texts(request):
            for holdout_id in renamed_ids:
                assert holdout_id not in text
