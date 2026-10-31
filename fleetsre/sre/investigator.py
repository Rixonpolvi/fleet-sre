"""Investigator: gated, budgeted loop ending in a diagnosis or NEEDS_INPUT."""

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fleetsre.sre.gateway import TransportError
from fleetsre.sre.schemas import render_incident
from fleetsre.sre.session import CallRecord, Session
from fleetsre.sre.tools import READ_TOOLS

NO_TOOL_MSG = "Plain text is not accepted. Call exactly one tool."
LAST_TURN_MSG = (
    "This is your last turn. Call submit_diagnosis "
    "now with what you have."
)
NOT_RUN_MSG = "not run: only the first tool call in a response runs"
STUCK_MSG = "stuck: the same tool call a third time in this segment"


@dataclass
class InvestigationResult:
    diagnosis: Any | None
    status: Literal["submitted", "needs_input"]
    reason: str
    transcript_id: str
    turns: int
    read_calls: int
    tokens: dict[str, Any] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)


def _prompt_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "prompts"
        / "investigate.md"
    )


def _read_prompt() -> str:
    return _prompt_path().read_text()


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


def _canonical(args: Any) -> str:
    if not isinstance(args, dict):
        return json.dumps(args, sort_keys=True, default=str)
    trimmed = {k: v for k, v in args.items() if k != "rationale"}
    return json.dumps(trimmed, sort_keys=True, default=str)


def _fresh_local_id(session: Session) -> str:
    n = 1
    while f"local-{n}" in session.calls:
        n += 1
    return f"local-{n}"


def _stuck_count(session: Session, name: str, args: Any) -> int:
    # Stuck guards repeated reads; working tools change session state,
    # so repeating them is progress, not a loop (T9 vs T10). Refused
    # calls never count, so three refused submissions reach the
    # refused cap instead of stuck (T8).
    if name not in READ_TOOLS:
        return 0
    canon = _canonical(args)
    return sum(
        1 for record in session.calls.values()
        if record.turn > session.segment_start_turn
        and record.verdict in ("accepted", "wouldRefuse")
        and record.tool == name
        and _canonical(record.args) == canon
    )


def _finish(
    session: Session,
    diagnosis: Any | None,
    status: Literal["submitted", "needs_input"],
    reason: str,
) -> InvestigationResult:
    tokens = {
        "input": int(session.tokens.get("input", 0)),
        "output": int(session.tokens.get("output", 0)),
        "cached": int(session.tokens.get("cached", 0)),
        "reasoning": int(session.tokens.get("reasoning", 0)),
    }
    return InvestigationResult(
        diagnosis=diagnosis,
        status=status,
        reason=reason,
        transcript_id=session.transcript_id,
        turns=session.turn,
        read_calls=session.read_calls,
        tokens=tokens,
        refusals=dict(session.refusals),
    )


def _run(
    incident: Any, tools: Any, gateway: Any, store: Any, session: Session
) -> InvestigationResult:
    role = gateway.config.roles["investigate"]
    prices = dict(gateway.config.prices.get(role.model, {}))
    history = session.history
    diagnosis: Any | None = None
    while session.turn < session.turn_limit:
        turn = session.turn + 1
        input_items = list(history)
        last_msg: dict[str, Any] | None = None
        if turn == session.turn_limit:
            last_msg = {"role": "user", "content": LAST_TURN_MSG}
            input_items.append(last_msg)
        try:
            response = gateway.respond(
                "investigate", input_items, tools.specs()
            )
        except TransportError:
            session.status = "needs_input"
            session.reason = "model_unavailable"
            store.save(session)
            return _finish(session, None, "needs_input", "model_unavailable")
        if last_msg is not None:
            history.append(last_msg)
        session.turn = turn
        items = [_clean_item(item) for item in response.output]
        history.extend(items)
        numbers = _usage_numbers(response.usage)
        cost = _cost_usd(numbers, prices)
        for key, value in (
            ("input", numbers["input_tokens"]),
            ("output", numbers["output_tokens"]),
            ("cached", numbers["cached_tokens"]),
            ("reasoning", numbers["reasoning_tokens"]),
        ):
            session.tokens[key] = int(session.tokens.get(key, 0)) + value
        session.segment_tokens += (
            numbers["input_tokens"] + numbers["output_tokens"]
        )
        usage = {
            "input_tokens": numbers["input_tokens"],
            "output_tokens": numbers["output_tokens"],
            "cached_tokens": numbers["cached_tokens"],
            "reasoning_tokens": numbers["reasoning_tokens"],
        }
        calls = [
            item for item in response.output
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        if not calls:
            history.append({"role": "user", "content": NO_TOOL_MSG})
            store.append_transcript(session.transcript_id, {
                "turn": turn, "items": copy.deepcopy(items),
                "tool": None, "args": {}, "verdict": "no_tool_call",
                "problems": [], "output": None,
                "usage": usage, "cost_usd": cost,
            })
            store.save(session)
            if session.segment_tokens > session.token_limit:
                session.status = "needs_input"
                session.reason = "token_budget"
                store.save(session)
                return _finish(
                    session, None, "needs_input", "token_budget"
                )
            continue
        first = calls[0]
        name = str(first.get("name", ""))
        try:
            args = json.loads(first.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        rest_outputs: list[dict[str, Any]] = []
        for index, extra in enumerate(calls[1:]):
            extra_id = extra.get("call_id") or f"unrun-{turn}-{index + 1}"
            rest_outputs.append({
                "type": "function_call_output",
                "call_id": extra_id,
                "output": json.dumps({
                    "callId": extra_id,
                    "result": {"error": NOT_RUN_MSG},
                }),
            })
        if _stuck_count(session, name, args) >= 2:
            call_id = first.get("call_id") or _fresh_local_id(session)
            result: dict[str, Any] = {"error": STUCK_MSG}
            session.calls[call_id] = CallRecord(
                tool=name, args=args, result=result, verdict="stuck",
                problems=[], turn=turn,
            )
            history.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(
                    {"callId": call_id, "result": result}
                ),
            })
            history.extend(rest_outputs)
            store.append_transcript(session.transcript_id, {
                "turn": turn, "items": copy.deepcopy(items),
                "tool": name, "args": copy.deepcopy(args),
                "verdict": "stuck", "problems": [],
                "output": {"callId": call_id, "result": result},
                "usage": usage, "cost_usd": cost,
            })
            session.status = "needs_input"
            session.reason = "stuck"
            store.save(session)
            return _finish(session, None, "needs_input", "stuck")
        if name in READ_TOOLS and (
            session.segment_reads >= session.read_limit
        ):
            call_id = first.get("call_id") or _fresh_local_id(session)
            problems = [
                "reads.cap: read budget exceeded: "
                f"{session.read_limit} read calls in this segment"
            ]
            result = {
                "error": "read budget exceeded", "problems": problems,
            }
            session.calls[call_id] = CallRecord(
                tool=name, args=args, result=result, verdict="refused",
                problems=problems, turn=turn,
            )
            session.refusals[name] = session.refusals.get(name, 0) + 1
            history.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(
                    {"callId": call_id, "result": result}
                ),
            })
            history.extend(rest_outputs)
            store.append_transcript(session.transcript_id, {
                "turn": turn, "items": copy.deepcopy(items),
                "tool": name, "args": copy.deepcopy(args),
                "verdict": "refused", "problems": list(problems),
                "output": {"callId": call_id, "result": result},
                "usage": usage, "cost_usd": cost,
            })
            store.save(session)
            if session.segment_tokens > session.token_limit:
                session.status = "needs_input"
                session.reason = "token_budget"
                store.save(session)
                return _finish(
                    session, None, "needs_input", "token_budget"
                )
            continue
        envelope = tools.call(
            name, args, session, call_id=first.get("call_id")
        )
        actual_id = envelope["callId"]
        record = session.calls[actual_id]
        verdict = record.verdict
        history.append({
            "type": "function_call_output",
            "call_id": first.get("call_id") or actual_id,
            "output": json.dumps(envelope),
        })
        history.extend(rest_outputs)
        if name in READ_TOOLS and verdict == "accepted":
            session.segment_reads += 1
            session.read_calls += 1
        if verdict == "refused":
            session.refusals[name] = session.refusals.get(name, 0) + 1
            if name == "submit_diagnosis":
                session.refused_submissions += 1
        store.append_transcript(session.transcript_id, {
            "turn": turn, "items": copy.deepcopy(items),
            "tool": name, "args": copy.deepcopy(args),
            "verdict": verdict, "problems": list(record.problems),
            "output": copy.deepcopy(envelope),
            "usage": usage, "cost_usd": cost,
        })
        store.save(session)
        if name == "submit_diagnosis" and verdict == "accepted":
            submitted = copy.deepcopy(args.get("diagnosis", {}))
            action_type = submitted.get("action", {}).get("type", "")
            diagnosis = submitted
            if action_type == "escalate":
                session.status = "needs_input"
                session.reason = "escalated"
                store.save(session)
                return _finish(
                    session, diagnosis, "needs_input", "escalated"
                )
            session.status = "submitted"
            session.reason = action_type
            store.save(session)
            return _finish(session, diagnosis, "submitted", action_type)
        if session.refused_submissions >= 3:
            session.status = "needs_input"
            session.reason = "refused"
            store.save(session)
            return _finish(session, None, "needs_input", "refused")
        if session.segment_tokens > session.token_limit:
            session.status = "needs_input"
            session.reason = "token_budget"
            store.save(session)
            return _finish(session, None, "needs_input", "token_budget")
    session.status = "needs_input"
    session.reason = "budget"
    store.save(session)
    return _finish(session, None, "needs_input", "budget")


def investigate(
    incident: Any,
    tools: Any,
    gateway: Any,
    store: Any,
    *,
    max_turns: int = 24,
    max_read_calls: int = 12,
    max_tokens: int = 400_000,
) -> InvestigationResult:
    """Run or continue an investigation (d5-pins.md pin 9)."""
    try:
        session = store.load(incident.id)
    except KeyError:
        session = None
    if session is None:
        session = Session(
            incident_id=incident.id,
            turn_limit=max_turns,
            read_limit=max_read_calls,
            token_limit=max_tokens,
            transcript_id=f"tx-{incident.id}",
            history=[
                {"role": "developer", "content": _read_prompt()},
                {
                    "role": "user",
                    "content": render_incident(
                        incident, tools.registry
                    ),
                },
            ],
        )
    elif session.status != "running":
        raise ValueError(
            f"investigation for {incident.id} already ended "
            f"({session.status}/{session.reason}); needs_input "
            "sessions resume only through resume()"
        )
    else:
        session.turn_limit = session.segment_start_turn + max_turns
        session.read_limit = max_read_calls
        session.token_limit = max_tokens
        store.save(session)
    return _run(incident, tools, gateway, store, session)


def resume(
    incident: Any,
    tools: Any,
    gateway: Any,
    store: Any,
    *,
    message: str,
    extra_turns: int = 8,
    extra_reads: int = 4,
) -> InvestigationResult:
    """Resume a paused investigation with a new segment (d5-pins.md pin 9)."""
    session = store.load(incident.id)
    session.segment_start_turn = session.turn
    session.turn_limit = session.turn + extra_turns
    session.read_limit = extra_reads
    session.segment_reads = 0
    session.segment_tokens = 0
    session.refused_submissions = 0
    session.resumed_at_turns.append(session.turn)
    session.status = "running"
    session.reason = ""
    session.history.append({"role": "user", "content": message})
    store.save(session)
    return _run(incident, tools, gateway, store, session)
