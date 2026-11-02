"""Authoring loop: brief -> contract -> scorecard -> feedback (d6b-pins 5-13)."""

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import yaml

from fleetsre.authoring.brief import ServiceBrief
from fleetsre.authoring.schema import authoring_schema
from fleetsre.contracts import Contract, ContractError, parse_contract
from fleetsre.harness.replay import T0
from fleetsre.harness.scenario import Scenario
from fleetsre.harness.scorecard import Scorecard, scorecard
from fleetsre.logmodel import LogEntry
from fleetsre.sre.gateway import ModelGateway, TransportError

ROLE = "author"


@dataclass
class AuthoringVersion:
    index: int
    status: Literal["invalid_json", "invalid_contract", "failed", "passed"]
    problems: list[str] = field(default_factory=list)
    contract: dict[str, Any] | None = None
    scorecard: Scorecard | None = None
    feedback: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass
class AuthoringResult:
    passed: bool
    stop_reason: Literal["passed", "max_revisions", "model_unavailable"]
    contract: Contract | None
    versions: list[AuthoringVersion] = field(default_factory=list)
    scorecards: list[Scorecard] = field(default_factory=list)
    model: str = ""
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0


def _prompt_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "prompts"
        / "author.md"
    )


def _clean_item(item: Any) -> Any:
    if isinstance(item, dict):
        return {k: v for k, v in item.items() if v is not None}
    return item


def _usage_numbers(usage: dict[str, Any]) -> dict[str, int]:
    details_in = usage.get("input_tokens_details") or {}
    details_out = usage.get("output_tokens_details") or {}
    if not isinstance(details_in, dict):
        details_in = {}
    if not isinstance(details_out, dict):
        details_out = {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cached_tokens": int(details_in.get("cached_tokens", 0)),
        "reasoning_tokens": int(details_out.get("reasoning_tokens", 0)),
    }


def _cost_usd(numbers: dict[str, int], prices: dict[str, Any]) -> float:
    uncapped = numbers["input_tokens"] - numbers["cached_tokens"]
    total = (
        uncapped * float(prices.get("input", 0.0))
        + numbers["cached_tokens"] * float(prices.get("cached_input", 0.0))
        + numbers["output_tokens"] * float(prices.get("output", 0.0))
    )
    return total / 1_000_000.0


def render_brief(
    brief: ServiceBrief,
    samples: list[LogEntry],
    schema: dict[str, Any],
    *,
    versions: int,
) -> str:
    """Build the first user message (d6b-pins 8)."""
    sections = [
        "# Service brief\n"
        + yaml.safe_dump(
            brief.model_dump(exclude={"sample_files"}), sort_keys=False
        ),
        "# Sample log entries\n"
        + "\n".join(
            json.dumps(entry.model_dump(mode="json"), separators=(",", ":"))
            for entry in samples
        ),
        "# Contract JSON schema\n" + json.dumps(schema, indent=2),
        "# Versions\n"
        f"You have {versions} versions. After each failed version you "
        "get feedback; reply with the whole revised contract.",
    ]
    return "\n\n".join(sections)


def _within_seconds(
    scenario: Scenario, kind: str, tripwire: str, deployment_id: str
) -> int:
    env = deployment_id.split("/", 1)[1]
    items = (
        scenario.expected.trips
        if kind == "trip"
        else scenario.expected.recoveries
    )
    for item in items:
        if item.deployment == env and item.tripwire == tripwire:
            return item.withinSeconds
    raise AssertionError(
        f"no expected {kind} for {tripwire} on {deployment_id}"
    )


def _render_missing(
    missing: list[tuple[str, str, str]], scenario: Scenario
) -> list[str]:
    rendered = []
    for kind, deployment, tripwire in missing:
        within = _within_seconds(scenario, kind, tripwire, deployment)
        if kind == "trip":
            rendered.append(
                f"trip {tripwire} on {deployment} "
                f"within {within} s of the fault"
            )
        else:
            rendered.append(
                f"recovery {tripwire} on {deployment} "
                f"within {within} s of the fault clearing"
            )
    return rendered


def _render_unexpected(unexpected: list[Any]) -> list[str]:
    rendered = []
    for event in unexpected:
        offset = int((event.at - T0).total_seconds())
        rendered.append(
            f"{event.kind} {event.tripwire} on {event.deployment} "
            f"at +{offset} s"
        )
    return rendered


def _join_capped(items: list[str]) -> str:
    if not items:
        return "none"
    text = ", ".join(items[:10])
    if len(items) > 10:
        text += f", and {len(items) - 10} more"
    return text


def render_feedback(
    version: AuthoringVersion, scenarios: list[Scenario]
) -> str:
    """Build the feedback message after a version (d6b-pins 10)."""
    n = version.index
    if version.status == "invalid_json":
        error = version.problems[0][len("json: "):]
        return (
            f"Version {n} is not valid JSON: {error}. "
            "Return the whole contract as one JSON object."
        )
    if version.status == "invalid_contract":
        rest = version.problems[0][len("contract."):]
        return (
            f"Version {n} was rejected by the contract loader: {rest}"
        )
    if version.status != "failed":
        raise ValueError(f"no feedback for a {version.status} version")
    if version.scorecard is None:
        raise ValueError("failed version has no scorecard")
    results = {r.scenario_id: r for r in version.scorecard.results}
    visible = [s for s in scenarios if not s.holdout]
    failing = [s for s in visible if not results[s.id].passed]
    if not failing:
        return f"Version {n} failed the scorecard: a held-out scenario failed."
    passed = len(visible) - len(failing)
    lines = [
        f"Version {n} failed the scorecard. "
        f"{passed} of {len(visible)} visible scenarios passed."
    ]
    for scenario in failing:
        result = results[scenario.id]
        missing = _join_capped(_render_missing(result.missing, scenario))
        unexpected = _join_capped(_render_unexpected(result.unexpected))
        lines.append(
            f"{scenario.id}: missing {missing}; unexpected {unexpected}"
        )
    return "\n".join(lines)


def version_scenarios(
    version: AuthoringVersion, scenarios: list[Scenario]
) -> list[dict[str, Any]]:
    """Pin-14 scenario entries for one version; empty when unscored."""
    if version.scorecard is None:
        return []
    results = {r.scenario_id: r for r in version.scorecard.results}
    entries = []
    for scenario in scenarios:
        result = results[scenario.id]
        entries.append({
            "scenario_id": scenario.id,
            "holdout": scenario.holdout,
            "passed": result.passed,
            "missing": _render_missing(result.missing, scenario),
            "unexpected": _render_unexpected(result.unexpected),
        })
    return entries


def _response_text(output: list[dict[str, Any]]) -> str:
    parts = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", [])
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    parts.append(part["text"])
    return "".join(parts)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped[3:]
    _line, sep, rest = body.partition("\n")
    if sep:
        body = rest
    body = body.strip()
    if body.endswith("```"):
        body = body[:-3]
    return body.strip()


def author_contract(
    brief: ServiceBrief,
    samples: list[LogEntry],
    scenarios: list[Scenario],
    gateway: ModelGateway,
    *,
    max_revisions: int = 3,
    scorer: Callable[[Contract, list[Scenario]], Scorecard] = scorecard,
    on_version: Callable[[AuthoringVersion], None] | None = None,
) -> AuthoringResult:
    """Run the authoring loop (d6b-pins 5-7, 9, 12-13).

    When on_version is set, it is called once per version as soon as
    that version is final: after its feedback is set, or for the last
    version, after it is appended.
    """
    schema = authoring_schema()
    text_format = {
        "type": "json_schema",
        "name": "contract",
        "schema": schema,
    }
    prompt = _prompt_path().read_text()
    role = gateway.config.roles[ROLE]
    prices = dict(gateway.config.prices.get(role.model, {}))
    model = role.model
    total_versions = max_revisions + 1
    input_items: list[dict[str, Any]] = [
        {"role": "developer", "content": prompt},
        {
            "role": "user",
            "content": render_brief(
                brief, samples, schema, versions=total_versions
            ),
        },
    ]
    versions: list[AuthoringVersion] = []
    scorecards: list[Scorecard] = []
    last_contract: Contract | None = None
    passing: Contract | None = None
    stop_reason: Literal["passed", "max_revisions", "model_unavailable"] = (
        "max_revisions"
    )
    for n in range(1, total_versions + 1):
        try:
            response = gateway.respond(
                ROLE, input_items, None, text_format=text_format
            )
        except TransportError:
            stop_reason = "model_unavailable"
            break
        numbers = _usage_numbers(response.usage)
        usage = {
            "input_tokens": numbers["input_tokens"],
            "output_tokens": numbers["output_tokens"],
            "cached_tokens": numbers["cached_tokens"],
            "reasoning_tokens": numbers["reasoning_tokens"],
        }
        cost = _cost_usd(numbers, prices)
        version, parsed = _read_version(
            n, response.output, brief, scenarios, model, usage, cost, scorer
        )
        if version.scorecard is not None:
            scorecards.append(version.scorecard)
        if version.status == "passed" and parsed is not None:
            passing = parsed
            last_contract = parsed
            versions.append(version)
            if on_version is not None:
                on_version(version)
            stop_reason = "passed"
            break
        if version.status == "failed" and parsed is not None:
            last_contract = parsed
        if n == total_versions:
            versions.append(version)
            if on_version is not None:
                on_version(version)
            break
        version.feedback = render_feedback(version, scenarios)
        versions.append(version)
        if on_version is not None:
            on_version(version)
        input_items.extend(
            _clean_item(item) for item in response.output
        )
        input_items.append({"role": "user", "content": version.feedback})
    tokens = {
        "input_tokens": sum(int(v.usage.get("input_tokens", 0)) for v in versions),
        "output_tokens": sum(
            int(v.usage.get("output_tokens", 0)) for v in versions
        ),
        "cached_tokens": sum(
            int(v.usage.get("cached_tokens", 0)) for v in versions
        ),
        "reasoning_tokens": sum(
            int(v.usage.get("reasoning_tokens", 0)) for v in versions
        ),
    }
    return AuthoringResult(
        passed=passing is not None,
        stop_reason=stop_reason,
        contract=passing if passing is not None else last_contract,
        versions=versions,
        scorecards=scorecards,
        model=model,
        tokens=tokens,
        cost_usd=sum(v.cost_usd for v in versions),
    )


def _read_version(
    n: int,
    output: list[dict[str, Any]],
    brief: ServiceBrief,
    scenarios: list[Scenario],
    model: str,
    usage: dict[str, int],
    cost: float,
    scorer: Callable[[Contract, list[Scenario]], Scorecard],
) -> tuple[AuthoringVersion, Contract | None]:
    """Read one model response as an AuthoringVersion (d6b-pins 9)."""
    text = _strip_fence(_response_text(output))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        error = str(exc)
        return (
            AuthoringVersion(
                index=n,
                status="invalid_json",
                problems=[f"json: {error}"],
                usage=dict(usage),
                cost_usd=cost,
            ),
            None,
        )
    if not isinstance(data, dict):
        return (
            AuthoringVersion(
                index=n,
                status="invalid_json",
                problems=["json: not an object"],
                usage=dict(usage),
                cost_usd=cost,
            ),
            None,
        )
    data["service"] = brief.service
    data["version"] = brief.version
    data["provenance"] = {
        "authoredBy": model,
        "scorecard": f"author-v{n}",
        "approvedBy": None,
    }
    try:
        contract = parse_contract(data)
    except ContractError as exc:
        return (
            AuthoringVersion(
                index=n,
                status="invalid_contract",
                problems=[f"contract.{exc.code}: {exc}"],
                contract=copy.deepcopy(data),
                usage=dict(usage),
                cost_usd=cost,
            ),
            None,
        )
    card = scorer(contract, scenarios)
    return (
        AuthoringVersion(
            index=n,
            status="passed" if card.passed else "failed",
            problems=[],
            contract=copy.deepcopy(data),
            scorecard=card,
            usage=dict(usage),
            cost_usd=cost,
        ),
        contract,
    )
