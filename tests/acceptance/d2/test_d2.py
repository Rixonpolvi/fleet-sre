"""D2 acceptance tests: SRE feasibility spike."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "fixtures" / "spike" / "green_liar"
SPIKE = ROOT / "spikes" / "sre_spike.py"
SCHEMA = ROOT / "spikes" / "diagnosis_min.schema.json"
RESULTS = ROOT / "spikes" / "RESULTS.md"

TOOLS = {
    "query_logs",
    "get_resource",
    "list_connector_logs",
    "test_integration_instance",
    "compare_siblings",
    "set_hypotheses",
    "update_plan",
    "record_evidence",
    "submit_diagnosis",
}
VERDICTS = {"accepted", "refused", "no_tool_call", "would_refuse"}


def _run_spike(*args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SPIKE), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _parse_lines(stdout: str) -> tuple[list[dict], list[dict], list[dict]]:
    traces, results, diagnoses = [], [], []
    for line in stdout.splitlines():
        if line.startswith("TRACE "):
            traces.append(json.loads(line[len("TRACE "):]))
        elif line.startswith("RESULT "):
            results.append(json.loads(line[len("RESULT "):]))
        elif line.startswith("DIAGNOSIS "):
            diagnoses.append(json.loads(line[len("DIAGNOSIS "):]))
    return traces, results, diagnoses


def _diagnosis_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def test_d2_t1_spike_runs_offline():
    """D2-T1: Scripted run exits 0 with one TRACE per turn and a valid DIAGNOSIS."""
    script = json.loads((FIXTURE / "scripted.json").read_text())
    proc = _run_spike(
        "--fixture", str(FIXTURE), "--model", "scripted", timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    traces, results, diagnoses = _parse_lines(proc.stdout)
    assert len(traces) == len(script)
    assert [t["turn"] for t in traces] == list(range(1, len(script) + 1))
    for trace in traces:
        assert trace["tool"] in TOOLS
        assert trace["verdict"] in VERDICTS
        assert isinstance(trace["rationale"], str) and trace["rationale"]
    assert all(t["verdict"] == "accepted" for t in traces)
    assert len(results) == 1
    assert results[0]["turns"] == len(script)
    assert results[0]["status"] == "accepted"
    assert results[0]["multiCallTurns"] == 0
    submits = [
        json.loads(turn[0]["arguments"])["diagnosis"]
        for turn in script
        if turn[0]["name"] == "submit_diagnosis"
    ]
    assert len(diagnoses) == 1
    assert diagnoses[0] == submits[-1]
    _diagnosis_validator().validate(diagnoses[0])


def _norm_ts(ts: str) -> str:
    if ts.endswith("Z") and "." not in ts:
        return ts[:-1] + ".000Z"
    return ts


def _window_bounds(window: str) -> tuple[str, str]:
    start, end = window.split("/")
    return _norm_ts(start), _norm_ts(end)


def _matches(
    entry: dict, env: str, start: str, end: str, filt: str, now: str
) -> bool:
    payload = entry["jsonPayload"]
    if payload.get("environment") != env:
        return False
    ts = entry["timestamp"]
    if not (start <= ts < end):
        return False
    if ts > now:
        return False
    if filt == "":
        return True
    if filt == "severity >= WARNING":
        return entry["severity"] in {"WARNING", "ERROR"}
    if filt.startswith("event = "):
        return payload.get("event") == filt[len("event = "):]
    raise AssertionError(f"test filter not implemented: {filt!r}")


def _expected_query(
    fixture_entries: list[dict],
    deployment: str,
    window: str,
    filt: str,
    now: str,
) -> list[dict]:
    env = deployment.split("/", 1)[1]
    start, end = _window_bounds(window)
    matched = [
        e for e in fixture_entries if _matches(e, env, start, end, filt, now)
    ]
    return sorted(matched, key=lambda e: e["timestamp"], reverse=True)


def _expected_signals(
    fixture_entries: list[dict], window: str, now: str
) -> dict[str, dict]:
    start, end = _window_bounds(window)
    out: dict[str, dict] = {}
    for env in ("alpha", "bravo", "charlie"):
        in_window = [
            e
            for e in fixture_entries
            if e["jsonPayload"].get("environment") == env
            and start <= e["timestamp"] < end
            and e["timestamp"] <= now
        ]
        completed = [
            e
            for e in in_window
            if e["jsonPayload"].get("event") == "connector.run.completed"
        ]
        warn_runs = {
            e["jsonPayload"]["runId"]
            for e in in_window
            if e["severity"] == "WARNING" and "runId" in e["jsonPayload"]
        }
        error_runs = {
            e["jsonPayload"]["runId"]
            for e in in_window
            if e["severity"] == "ERROR" and "runId" in e["jsonPayload"]
        }
        alerts = sum(
            1
            for e in in_window
            if e["jsonPayload"].get("event") == "alert.ingested"
        )
        out[f"alert-ingestion/{env}"] = {
            "runs": len(completed),
            "warningRuns": len(warn_runs),
            "errorRuns": len(error_runs),
            "alertsIngested": alerts,
            "lastRunAt": max(e["timestamp"] for e in completed),
        }
    return out


def test_d2_t2_stub_tools_are_faithful():
    """D2-T2: Stub tools return capped fixture data computed over the window."""
    sys.path.insert(0, str(ROOT / "spikes"))
    try:
        import sre_spike
    finally:
        sys.path.pop(0)
    tools = sre_spike.StubTools(FIXTURE)
    fixture_entries = [
        json.loads(line)
        for line in (FIXTURE / "logs.jsonl").read_text().splitlines()
        if line.strip()
    ]
    resources = json.loads((FIXTURE / "resources.json").read_text())
    incident = json.loads((FIXTURE / "incident.json").read_text())
    now = _norm_ts(incident["now"])

    full = "2026-10-29T14:00:00Z/2026-10-29T16:00:00Z"
    expected_wide = _expected_query(
        fixture_entries, "alert-ingestion/bravo", full, "", now
    )
    assert len(expected_wide) > 50
    wide = tools.query_logs(
        deployment="alert-ingestion/bravo",
        window=full,
        filter="",
        rationale="faithfulness check",
    )
    assert wide["total"] == len(expected_wide)
    assert wide["truncated"] is True
    assert wide["entries"] == expected_wide[:50]

    narrow_window = "2026-10-29T15:00:00Z/2026-10-29T15:05:00Z"
    expected_narrow = _expected_query(
        fixture_entries,
        "alert-ingestion/bravo",
        narrow_window,
        "severity >= WARNING",
        now,
    )
    assert 0 < len(expected_narrow) <= 50
    narrow = tools.query_logs(
        deployment="alert-ingestion/bravo",
        window=narrow_window,
        filter="severity >= WARNING",
        rationale="faithfulness check",
    )
    assert narrow["total"] == len(expected_narrow)
    assert narrow["truncated"] is False
    assert narrow["entries"] == expected_narrow

    past_window = "2026-10-29T14:00:00Z/2026-10-29T17:00:00Z"
    expected_cut = _expected_query(
        fixture_entries, "alert-ingestion/bravo", past_window, "", now
    )
    expected_nocut = _expected_query(
        fixture_entries,
        "alert-ingestion/bravo",
        past_window,
        "",
        "9999-12-31T23:59:59.999Z",
    )
    assert len(expected_cut) < len(expected_nocut)
    past_now = tools.query_logs(
        deployment="alert-ingestion/bravo",
        window=past_window,
        filter="",
        rationale="faithfulness check",
    )
    assert past_now["total"] == len(expected_cut)
    assert past_now["entries"] == expected_cut[:50]
    assert all(e["timestamp"] <= now for e in past_now["entries"])

    connector = resources["connectorInstances"][0]
    assert tools.get_resource(
        connector["name"], rationale="faithfulness check"
    ) == connector
    integration = resources["integrationInstances"][1]
    assert tools.get_resource(
        integration["name"], rationale="faithfulness check"
    ) == integration

    now_ms = int(
        datetime.fromisoformat(incident["now"].replace("Z", "+00:00")).timestamp()
        * 1000
    )
    bravo_logs = resources["connectorLogs"]["bravo-sentinel"]
    bravo_expected = [r for r in bravo_logs if int(r["time"]) <= now_ms]
    assert len(bravo_expected) > 50
    assert len(bravo_expected) < len(bravo_logs)
    listed = tools.list_connector_logs(
        "bravo-sentinel", rationale="faithfulness check"
    )
    assert listed["total"] == len(bravo_expected)
    assert listed["truncated"] is True
    assert listed["logs"] == bravo_expected[:50]
    alpha_expected = [
        r
        for r in resources["connectorLogs"]["alpha-sentinel"]
        if int(r["time"]) <= now_ms
    ]
    alpha_listed = tools.list_connector_logs(
        "alpha-sentinel", rationale="faithfulness check"
    )
    assert alpha_listed["total"] == len(alpha_expected)
    assert alpha_listed["truncated"] is (len(alpha_expected) > 50)
    assert alpha_listed["logs"] == alpha_expected[:50]

    assert tools.test_integration_instance(
        resources["integrationInstances"][1]["name"],
        rationale="faithfulness check",
    ) == resources["executeTest"]["bravo"]

    siblings = tools.compare_siblings(
        service="alert-ingestion",
        component="connector",
        window=full,
        rationale="faithfulness check",
    )
    assert siblings["window"] == full
    assert siblings["deployments"] == _expected_signals(
        fixture_entries, full, now
    )
    assert (
        siblings["deployments"]["alert-ingestion/alpha"]["warningRuns"] == 0
    )
    assert (
        siblings["deployments"]["alert-ingestion/charlie"]["warningRuns"] == 0
    )


def test_d2_t3_gates_refuse_in_the_loop():
    """D2-T3: Exact refusal turns, then the run continues to accepted."""
    script = json.loads((FIXTURE / "scripted_refusals.json").read_text())
    proc = _run_spike(
        "--fixture",
        str(FIXTURE),
        "--model",
        "scripted",
        "--script",
        str(FIXTURE / "scripted_refusals.json"),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    traces, results, diagnoses = _parse_lines(proc.stdout)
    assert len(traces) == len(script)
    assert [t["turn"] for t in traces] == list(range(1, len(script) + 1))
    for trace in traces:
        if trace["turn"] == 2:
            assert trace["tool"] == "record_evidence"
            assert trace["verdict"] == "refused"
        elif trace["turn"] == 3:
            assert trace["tool"] == "submit_diagnosis"
            assert trace["verdict"] == "refused"
        else:
            assert trace["verdict"] == "accepted"
    assert len(results) == 1
    assert results[0]["status"] == "accepted"
    assert results[0]["multiCallTurns"] == 0
    submits = [
        json.loads(turn[0]["arguments"])["diagnosis"]
        for turn in script
        if turn[0]["name"] == "submit_diagnosis"
    ]
    assert len(diagnoses) == 1
    assert diagnoses[0] == submits[-1]
    _diagnosis_validator().validate(diagnoses[0])


def _needs_live_key() -> None:
    if "MODEL_API_KEY" not in os.environ:
        pytest.skip("MODEL_API_KEY is not set; live spike test needs a model key")


@pytest.mark.live
def test_d2_t4_live_diagnosis():
    """D2-T4: Live model names bravo/credential_failure in 2 of 3 runs."""
    _needs_live_key()
    proc = _run_spike("--fixture", str(FIXTURE), "--runs", "3", timeout=1500)
    assert proc.returncode == 0, proc.stderr
    traces, results, _diagnoses = _parse_lines(proc.stdout)
    assert len(results) == 3
    good = 0
    blocks = _split_runs(proc.stdout)
    assert len(blocks) == 3
    for traces_b, results_b, diagnoses_b in blocks:
        assert len(results_b) == 1
        assert "multiCallTurns" in results_b[0]
        turns = [t["turn"] for t in traces_b]
        assert turns == list(range(1, len(traces_b) + 1))
        assert len(traces_b) <= 24
        for trace in traces_b:
            if trace["verdict"] == "no_tool_call":
                continue
            assert isinstance(trace["tool"], str) and trace["tool"] in TOOLS
        if (
            results_b[0].get("status") == "accepted"
            and results_b[0]["multiCallTurns"] == 0
            and len(diagnoses_b) == 1
        ):
            diagnosis = diagnoses_b[0]
            _diagnosis_validator().validate(diagnosis)
            if (
                diagnosis["deployment"] == "alert-ingestion/bravo"
                and diagnosis["category"] == "credential_failure"
            ):
                good += 1
    assert good >= 2


@pytest.mark.live
def test_d2_t5_cost_and_protocol_recorded():
    """D2-T5: Each live run appends cost and protocol rows to RESULTS.md."""
    _needs_live_key()
    before = RESULTS.stat().st_size if RESULTS.exists() else 0
    proc = _run_spike("--fixture", str(FIXTURE), "--runs", "1", timeout=600)
    assert proc.returncode == 0, proc.stderr
    assert RESULTS.exists()
    tail = RESULTS.read_text()[before:].lower()
    assert len(tail) > 0
    for keyword in (
        "model",
        "token",
        "reasoning",
        "cach",
        "turn",
        "read",
        "refus",
        "latenc",
        "cost",
    ):
        assert keyword in tail, f"RESULTS.md entry lacks {keyword!r}"


def _split_runs(stdout: str) -> list[tuple[list[dict], list[dict], list[dict]]]:
    blocks: list[tuple[list[dict], list[dict], list[dict]]] = []
    traces, results, diagnoses = _parse_lines(stdout)
    run_ids = sorted({t["run"] for t in traces} | {r["run"] for r in results})
    run_of_diagnosis: list[int] = []
    current: int | None = None
    for line in stdout.splitlines():
        if line.startswith("TRACE "):
            current = json.loads(line[len("TRACE "):])["run"]
        elif line.startswith("RESULT "):
            current = json.loads(line[len("RESULT "):])["run"]
        elif line.startswith("DIAGNOSIS "):
            run_of_diagnosis.append(current if current is not None else -1)
    diag_by_run: dict[int, list[dict]] = {}
    for run_id, diag in zip(run_of_diagnosis, diagnoses):
        diag_by_run.setdefault(run_id, []).append(diag)
    for run_id in run_ids:
        blocks.append((
            [t for t in traces if t["run"] == run_id],
            [r for r in results if r["run"] == run_id],
            diag_by_run.get(run_id, []),
        ))
    return blocks
