"""Pure view functions for the demo web app (d8-pins 8, 12-15)."""

from datetime import timedelta
from typing import Any

READ_TOOLS = frozenset({
    "query_logs",
    "get_resource",
    "list_connector_logs",
    "list_job_logs",
    "test_integration_instance",
    "compare_siblings",
    "tripwire_history",
    "get_contract",
})


def _transcript_records(rt: Any, record: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for transcript_id in record.transcript_ids:
        out.extend(rt.store.transcript(transcript_id))
    return out


def _elide_encrypted(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            key: (
                f"<encrypted: {len(value)} chars>"
                if key == "encrypted_content"
                and isinstance(value, str)
                else _elide_encrypted(value)
            )
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_elide_encrypted(item) for item in obj]
    return obj


def _load_session(rt: Any, incident_id: str) -> Any | None:
    try:
        return rt.store.load(incident_id)
    except KeyError:
        return None


def _hypotheses_view(session: Any) -> list[dict[str, Any]]:
    if session is None:
        return []
    return [
        {
            "id": hypothesis.id,
            "statement": hypothesis.statement,
            "wouldConfirm": hypothesis.wouldConfirm,
            "alternative": hypothesis.alternative,
        }
        for hypothesis in session.hypotheses.values()
    ]


def _evidence_view(rt: Any, session: Any) -> list[dict[str, Any]]:
    if session is None:
        return []
    from fleetsre.ports import MemoryLogQuery

    log_query = MemoryLogQuery(rt.sink)
    items: list[dict[str, Any]] = []
    for recorded in session.evidence.values():
        if recorded.insertId is not None:
            entry = log_query.get(recorded.insertId)
            if entry is None:
                source: dict[str, Any] = {
                    "kind": "log", "insertId": recorded.insertId,
                }
            else:
                source = {
                    "kind": "log",
                    "insertId": recorded.insertId,
                    "timestamp": entry.timestamp.isoformat(),
                    "severity": entry.severity,
                    "event": entry.jsonPayload.get("event", ""),
                    "message": entry.jsonPayload.get("message", ""),
                    "deployment": recorded.deployment or "",
                }
        else:
            call = session.calls.get(recorded.callId or "")
            source = {
                "kind": "toolResult",
                "callId": recorded.callId,
                "tool": call.tool if call is not None else "",
                "field": recorded.field,
                "value": recorded.value,
            }
        items.append({
            "id": recorded.id,
            "hypothesis": recorded.hypothesis,
            "stance": recorded.stance,
            "why": recorded.why,
            "recordedAtTurn": recorded.recordedAtTurn,
            "source": source,
        })
    return items


def _proposal_view(proposal: Any) -> dict[str, Any]:
    method = proposal.method
    if method.endswith(".restart"):
        return {
            "hash": proposal.hash,
            "seq": proposal.seq,
            "method": method,
            "resource": proposal.resource,
            "update_mask": list(proposal.update_mask),
            "diff": [],
            "steps": "enabled: false \u2192 true",
        }
    if method.endswith(".runOnDemand"):
        return {
            "hash": proposal.hash,
            "seq": proposal.seq,
            "method": method,
            "resource": proposal.resource,
            "update_mask": list(proposal.update_mask),
            "diff": [],
            "steps": "run once",
        }
    before_resource: dict[str, Any] = getattr(
        proposal, "resource_before", {}
    ) or {}
    patch: dict[str, Any] = proposal.patch or {}
    mask: list[str] = list(proposal.update_mask or [])
    diff: list[dict[str, Any]] = []
    if "parameters" in mask:
        proposed = patch.get("parameters", [])
        current = {
            param.get("displayName"): param
            for param in before_resource.get("parameters", [])
        }
        for param in proposed:
            display = param.get("displayName", "")
            have = current.get(display, {})
            before_value = str(have.get("value", ""))
            after_value = str(param.get("value", ""))
            if have.get("type") == "PASSWORD":
                before_value = "********"
                after_value = "********"
            if before_value == after_value:
                continue
            diff.append({
                "field": f"parameters[{display}]",
                "before": before_value,
                "after": after_value,
            })
    for field in mask:
        if field == "parameters":
            continue
        before_value = before_resource.get(field, "")
        after_value = patch.get(field, "")
        if before_value == after_value:
            continue
        diff.append({
            "field": field,
            "before": before_value,
            "after": after_value,
        })
    return {
        "hash": proposal.hash,
        "seq": proposal.seq,
        "method": method,
        "resource": proposal.resource,
        "update_mask": mask,
        "diff": diff,
        "steps": None,
    }


def _verification_view(rt: Any, record: Any) -> dict[str, Any] | None:
    started_index: int | None = None
    tripwire = ""
    seconds = 0
    for index, event in enumerate(record.timeline):
        if event.kind == "verification_started":
            started_index = index
            tripwire = str(event.detail.get("tripwire", ""))
            seconds = int(event.detail.get("seconds", 0))
    if started_index is None:
        return None
    started_event = record.timeline[started_index]
    started_at = started_event.at
    outcome = "pending"
    for event in record.timeline[started_index + 1:]:
        if event.kind == "verified":
            outcome = "verified"
        elif event.kind == "verification_failed":
            outcome = "failed"
        elif event.kind == "verification_started":
            outcome = "pending"
    deployment = record.incident.deployment
    status = rt.evaluator.status().get(deployment, {})
    return {
        "tripwire": tripwire,
        "seconds": seconds,
        "started_at": started_at.isoformat(),
        "deadline": (started_at + timedelta(seconds=seconds)).isoformat(),
        "current": status.get(tripwire, "unknown"),
        "outcome": outcome,
    }


def _turns_view(
    records: list[dict[str, Any]], session: Any
) -> list[dict[str, Any]]:
    resumes = sorted(session.resumed_at_turns) if session is not None else []
    cards: list[dict[str, Any]] = []
    for stored in records:
        turn = stored["turn"]
        segment = 1 + sum(1 for point in resumes if point < turn)
        tool = stored.get("tool")
        args = stored.get("args") or {}
        if tool is None:
            think = ""
            act = None
            observe = None
        else:
            think = args.get("rationale", "") if isinstance(args, dict) else ""
            act = {
                "tool": tool,
                "args": {
                    key: value for key, value in args.items()
                    if key != "rationale"
                },
            }
            output = stored.get("output")
            if isinstance(output, dict) and "result" in output:
                observe = output["result"]
            else:
                observe = output
        cards.append({
            "n": turn,
            "segment": segment,
            "think": think,
            "act": act,
            "observe": observe,
            "verdict": stored.get("verdict"),
            "usage": stored.get("usage"),
        })
    return cards


def _header_view(records: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = {
        key: sum(
            (record.get("usage") or {}).get(key, 0) for record in records
        )
        for key in (
            "input_tokens", "output_tokens", "reasoning_tokens",
            "cached_tokens",
        )
    }
    refusals: dict[str, int] = {}
    for record in records:
        if record.get("verdict") == "refused":
            for problem in record.get("problems") or []:
                rule = str(problem).split(":", 1)[0]
                refusals[rule] = refusals.get(rule, 0) + 1
    return {
        "turns": len(records),
        "model_calls": len(records),
        "read_calls": sum(
            1 for record in records
            if record.get("verdict") == "accepted"
            and record.get("tool") in READ_TOOLS
        ),
        "tokens": tokens,
        "cost_usd": sum(record.get("cost_usd", 0.0) for record in records),
        "refusals": refusals,
    }


def _handoff_view(
    rt: Any, record: Any, session: Any, records: list[dict[str, Any]]
) -> dict[str, Any] | None:
    state = record.state.value if hasattr(record.state, "value") else str(
        record.state
    )
    if state != "NEEDS_INPUT":
        return None
    last_refused = next(
        (item for item in reversed(records)
         if item.get("verdict") == "refused"),
        None,
    )
    if last_refused is None:
        last_refusal = None
    else:
        last_refusal = {
            "n": last_refused["turn"],
            "tool": last_refused.get("tool"),
            "problems": list(last_refused.get("problems") or []),
        }
    reason = record.reason
    escalation_summary = None
    if reason == "escalated" and record.diagnosis:
        escalation_summary = (record.diagnosis.get("action") or {}).get(
            "escalationSummary"
        )
    paused_at = record.paused_at
    if paused_at is None:
        since_pause = 0
        paused_text = None
    else:
        since_pause = sum(
            1 for at in rt.call_times if at > paused_at
        )
        paused_text = paused_at.isoformat()
    return {
        "reason": reason,
        "hypotheses": _hypotheses_view(session),
        "evidence": _evidence_view(rt, session),
        "last_refusal": last_refusal,
        "escalation_summary": escalation_summary,
        "paused_at": paused_text,
        "model_calls_since_pause": since_pause,
    }


def fleet_view(rt: Any) -> dict[str, Any]:
    """Grid data: act, now, held, services x deployments (d8-pins 12)."""
    act_name = getattr(rt, "act_name", "") or ""
    status = rt.evaluator.status()
    ordered: list[Any] = list(rt.evaluator.deployments)
    services: list[dict[str, Any]] = []
    by_service: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for deployment in ordered:
        contract = rt.registry.contract(deployment.service)
        dep_status = status.get(deployment.id, {})
        components: list[dict[str, Any]] = []
        for component in contract.components:
            tripwires = [
                {
                    "id": tripwire.id,
                    "status": dep_status.get(tripwire.id, "ok"),
                }
                for tripwire in contract.tripwires
                if tripwire.component == component.id
            ]
            health = (
                "tripped"
                if any(item["status"] == "tripped" for item in tripwires)
                else "ok"
            )
            components.append({
                "id": component.id,
                "kind": component.kind,
                "health": health,
                "tripwires": tripwires,
            })
        dep_health = (
            "tripped"
            if any(item["health"] == "tripped" for item in components)
            else "ok"
        )
        candidates = [
            item for item in rt.manager.incidents()
            if item.incident.deployment == deployment.id
        ]
        if candidates:
            latest = max(
                candidates,
                key=lambda item: (item.incident.opened_at, item.id),
            )
            incident: dict[str, Any] | None = {
                "id": latest.id,
                "state": latest.state.value
                if hasattr(latest.state, "value") else str(latest.state),
            }
        else:
            incident = None
        view = {
            "id": deployment.id,
            "environment": deployment.environment,
            "health": dep_health,
            "incident": incident,
            "components": components,
        }
        if deployment.service not in by_service:
            by_service[deployment.service] = []
            order.append(deployment.service)
        by_service[deployment.service].append(view)
    for service in order:
        services.append({
            "service": service, "deployments": by_service[service],
        })
    return {
        "act": act_name,
        "now": rt.clock.now().isoformat(),
        "held": bool(rt.held),
        "services": services,
    }


def incident_view(rt: Any, incident_id: str) -> dict[str, Any]:
    """Incident data: hypotheses, evidence, proposal, turns, handoff (13-15)."""
    record = rt.manager.get(incident_id)
    session = _load_session(rt, incident_id)
    records = _transcript_records(rt, record)
    proposals = list(record.proposals or [])
    timeline = [
        event.model_dump(mode="json")
        if hasattr(event, "model_dump") else dict(event)
        for event in record.timeline
    ]
    postmortem = record.postmortem
    return {
        "id": record.id,
        "deployment": record.incident.deployment,
        "state": record.state.value
        if hasattr(record.state, "value") else str(record.state),
        "reason": record.reason,
        "tripwires": list(record.tripwires),
        "timeline": timeline,
        "postmortem": (
            postmortem.model_dump(mode="json")
            if postmortem is not None else None
        ),
        "hypotheses": _hypotheses_view(session),
        "evidence": _evidence_view(rt, session),
        "diagnosis": record.diagnosis,
        "proposal": _proposal_view(proposals[-1]) if proposals else None,
        "verification": _verification_view(rt, record),
        "turns": _turns_view(records, session),
        "header": _header_view(records),
        "handoff": _handoff_view(rt, record, session, records),
    }


def turn_input(rt: Any, incident_id: str, turn: int) -> list[dict[str, Any]]:
    """Exact model input of turn n, encrypted_content elided (d8-pins 14).

    Only the act's own incident is accepted: the request log is per
    runtime and each act has one incident, so any other ID is an error.
    """
    if incident_id != rt.act.incident:
        raise ValueError(f"unknown incident: {incident_id}")
    record = rt.manager.get(incident_id)
    records = _transcript_records(rt, record)
    seen = {item["turn"] for item in records}
    if turn not in seen:
        raise ValueError(f"unknown turn: {turn}")
    transport = rt.transport
    if transport is not None:
        requests = transport.requests
    else:
        requests = rt._counting.requests
    if turn < 1 or turn > len(requests):
        raise ValueError(f"unknown turn: {turn}")
    return _elide_encrypted(requests[turn - 1]["input"])
