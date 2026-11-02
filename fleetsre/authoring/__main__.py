"""Authoring CLI: brief -> contract revisions -> scorecard (d6b-pins 14).

Usage:
    python -m fleetsre.authoring --brief <path> [--runs N]
        [--max-revisions 3] [--script <file>] [--out <path>]
"""

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUTHOR_MODEL_DEFAULT = "muse-wonder-1.0"
AUTHOR_EFFORT_DEFAULT = "high"
AUTHOR_TIMEOUT_S = 300.0
BASE_URL = "https://api.meta.ai/v1"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _author_model() -> str:
    return os.environ.get("FLEETSRE_AUTHOR_MODEL", AUTHOR_MODEL_DEFAULT)


def _author_effort() -> str:
    return os.environ.get("FLEETSRE_AUTHOR_EFFORT", AUTHOR_EFFORT_DEFAULT)


def _price_for(model: str) -> dict[str, float] | None:
    raw = os.environ.get("FLEETSRE_PRICES_JSON", "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get(model), dict):
        return dict(data[model])
    return None


def _version_line(
    run: int, version: Any, scenarios: list[Any]
) -> dict[str, Any]:
    visible = [s for s in scenarios if not s.holdout]
    holdouts = [s for s in scenarios if s.holdout]
    visible_passed = 0
    holdouts_passed = 0
    false_positives = 0
    if version.scorecard is not None:
        by_id = {r.scenario_id: r for r in version.scorecard.results}
        visible_passed = sum(
            1 for s in visible if by_id[s.id].passed
        )
        holdouts_passed = sum(
            1 for s in holdouts if by_id[s.id].passed
        )
        false_positives = version.scorecard.false_positives
    return {
        "run": run,
        "version": version.index,
        "status": version.status,
        "visiblePassed": visible_passed,
        "visibleTotal": len(visible),
        "holdoutsPassed": holdouts_passed,
        "falsePositives": false_positives,
        "problems": version.problems,
        "costUsd": version.cost_usd,
    }


def _default_out() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _repo_root() / "evals" / "authoring" / f"{stamp}.json"


def _write_out(path: Path, runs: list[dict[str, Any]]) -> None:
    """Write {"runs": [...]} atomically via a temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps({"runs": runs}, indent=2) + "\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    from fleetsre.authoring.brief import load_brief
    from fleetsre.authoring.loop import (
        author_contract,
        version_scenarios,
    )
    from fleetsre.harness.scenario import load_scenario
    from fleetsre.logmodel import read_jsonl
    from fleetsre.sre.gateway import (
        GatewayConfig,
        ModelGateway,
        OpenAITransport,
        RoleConfig,
        ScriptedTransport,
    )

    parser = argparse.ArgumentParser(description="Author a contract")
    parser.add_argument("--brief", required=True, help="brief YAML path")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--max-revisions", type=int, default=3)
    parser.add_argument("--script", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    root = _repo_root()
    brief = load_brief(args.brief)
    samples = []
    for rel in brief.sample_files:
        samples.extend(read_jsonl(root / rel))
    scen_dir = root / "fixtures" / "scenarios" / brief.service
    scenarios = [
        load_scenario(path) for path in sorted(scen_dir.glob("*.yaml"))
    ]
    if not scenarios:
        print(f"no scenarios for {brief.service}", file=sys.stderr)
        return 2

    model = _author_model()
    scripted: list[dict[str, Any]] | None = None
    if args.script is not None:
        with open(args.script, encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            scripted = [payload]
        elif isinstance(payload, list):
            scripted = payload
        else:
            print("script file must hold a response or a list",
                  file=sys.stderr)
            return 2
    else:
        api_key = os.environ.get("MODEL_API_KEY", "")
        if not api_key:
            print("MODEL_API_KEY is not set; refusing a live run",
                  file=sys.stderr)
            return 2

    price = _price_for(model)
    if args.script is None and price is None:
        print(f"no price for author model {model}", file=sys.stderr)
        return 2
    prices = {model: price} if price is not None else {}
    config = GatewayConfig(
        roles={"author": RoleConfig(
            model=model,
            reasoning_effort=_author_effort(),
            timeout_s=AUTHOR_TIMEOUT_S,
            retry_timeouts=False,
        )},
        base_url=BASE_URL,
        max_retries=5,
        prices=prices,
    )

    out_path = Path(args.out) if args.out else _default_out()
    runs_out: list[dict[str, Any]] = []
    for run in range(1, args.runs + 1):
        if scripted is not None:
            transport: Any = ScriptedTransport(
                copy.deepcopy(scripted)
            )
        else:
            transport = OpenAITransport(
                api_key=os.environ.get("MODEL_API_KEY", ""),
                base_url=BASE_URL,
            )
        gateway = ModelGateway(config, transport)

        def _emit(version: Any, _run: int = run) -> None:
            print(
                "VERSION " + json.dumps(
                    _version_line(_run, version, scenarios)
                ),
                flush=True,
            )

        result = author_contract(
            brief, samples, scenarios, gateway,
            max_revisions=args.max_revisions, on_version=_emit,
        )
        print("RESULT " + json.dumps({
            "run": run,
            "passed": result.passed,
            "stopReason": result.stop_reason,
            "versions": len(result.versions),
            "costUsd": result.cost_usd,
        }))
        runs_out.append({
            "run": run,
            "passed": result.passed,
            "stop_reason": result.stop_reason,
            "contract": (
                result.contract.model_dump(mode="json")
                if result.contract is not None else None
            ),
            "tokens": result.tokens,
            "cost_usd": result.cost_usd,
            "versions": [
                {
                    "version": version.index,
                    "status": version.status,
                    "problems": version.problems,
                    "feedback": version.feedback,
                    "usage": version.usage,
                    "cost_usd": version.cost_usd,
                    "scenarios": version_scenarios(version, scenarios),
                }
                for version in result.versions
            ],
        })
        _write_out(out_path, runs_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
