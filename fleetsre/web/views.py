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


_TRIGGER_WORDS = {
    "opened": "tripwire tripped",
    "reply": "you replied",
    "tripwire": "tripwire state changed",
    "rejection": "you rejected the proposal",
    "conflict": "resource changed since the proposal",
    "invalid_patch": "proposal was invalid",
    "apply_failed": "apply failed",
    "verification_failed": "verification failed",
}


_SEGMENT_WORDS = {
    "start": "first investigation",
    "resume": "resumed investigation",
    "reopen": "reopened investigation",
}


def trigger_words(trigger: Any) -> str:
    """A pending/resumed trigger code in plain words."""
    text = str(trigger or "")
    if text in _TRIGGER_WORDS:
        return _TRIGGER_WORDS[text]
    return text.replace("_", " ") or text


def segment_words(segment: Any) -> str:
    """A segment kind in plain words."""
    text = str(segment or "")
    return _SEGMENT_WORDS.get(text, text.replace("_", " ") or text)


def _activity_snapshot(rt: Any) -> dict[str, Any] | None:
    activity = getattr(rt, "activity", None)
    if activity is None:
        return None
    try:
        snapshot = activity.snapshot()
    except (TypeError, ValueError, AttributeError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _demo_model(rt: Any) -> str:
    try:
        return str(
            rt.gateway.config.roles["investigate"].model
        )
    except (AttributeError, KeyError, TypeError):
        return ""


def display_model(rt: Any) -> str:
    """Model name for humans: scripted replay unless live."""
    try:
        live = bool(getattr(rt, "live", False))
    except (AttributeError, TypeError):
        live = False
    if not live:
        return "scripted replay"
    return _demo_model(rt)


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
        params = getattr(deployment, "parameters", {}) or {}
        view = {
            "id": deployment.id,
            "environment": deployment.environment,
            "health": dep_health,
            "incident": incident,
            "components": components,
            "expected_alerts_per_hour": params.get(
                "expectedAlertsPerHour"
            ),
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


def _hhmmss(value: Any) -> str:
    """A sim timestamp as HH:MM:SS for banner sentences."""
    if hasattr(value, "strftime"):
        try:
            return str(value.strftime("%H:%M:%S"))
        except (TypeError, ValueError):
            pass
    text = str(value)
    if len(text) >= 19 and text[10:11] == "T":
        return text[11:19]
    return text


def _rotatable_faults(rt: Any, record: Any) -> list[dict[str, Any]]:
    """Active credential_expiry faults on the incident's environment."""
    deployment = str(record.incident.deployment)
    environment = deployment.split("/", 1)[-1]
    return [
        fault for fault in rt.active_faults()
        if fault["type"] == "credential_expiry"
        and fault["environment"] == environment
    ]


def _demo_since_pause(record: Any) -> bool:
    """Whether the credential was rotated since the pause began."""
    paused_at = record.paused_at
    for event in record.timeline:
        if event.kind == "demo" and (
            paused_at is None or event.at >= paused_at
        ):
            return True
    return False


def _tripped_names(record: Any) -> list[str]:
    """Tripwires tripped at pause time, falling back to the incident's."""
    snapshot = record.paused_snapshot or {}
    tripped = sorted(
        name for name, status in snapshot.items() if status == "tripped"
    )
    return tripped or list(record.tripwires or [])


def handoff_actions(record: Any, rt: Any) -> dict[str, Any] | None:
    """Which NEEDS_INPUT controls to show: one primary plus other options.

    Returns None outside NEEDS_INPUT. ``primary`` is one of "rotate",
    "grant", "note" or None (after rotation there is no primary button);
    ``others`` lists the controls under the collapsed "Other options".
    """
    state = (
        record.state.value
        if hasattr(record.state, "value") else str(record.state)
    )
    if state != "NEEDS_INPUT":
        return None
    reason = record.reason or ""
    rotatable = _rotatable_faults(rt, record)
    rotated = _demo_since_pause(record)
    if rotated:
        primary = None
    elif reason == "escalated" and rotatable:
        primary = "rotate"
    elif reason in ("budget", "token_budget"):
        primary = "grant"
    else:
        primary = "note"
    if primary == "note":
        others = ["close"]
    else:
        others = ["note", "close"]
    if reason in ("budget", "token_budget") and primary != "grant":
        others.append("grant")
    return {
        "primary": primary,
        "others": others,
        "rotatable": rotatable,
        "tripped": _tripped_names(record),
        "rotated": rotated,
    }


def next_step(rt: Any, record: Any) -> dict[str, str]:
    """Status banner: who acts next and what they do (d8-story)."""
    snapshot = _activity_snapshot(rt)
    if snapshot is not None and snapshot.get("state") == "running":
        incident = snapshot.get("incident")
        if incident is None or incident == getattr(record, "id", None):
            turns = snapshot.get("turns") or []
            try:
                count = len(turns)
            except TypeError:
                count = 0
            return {
                "actor": "sre",
                "text": f"investigating live \u2014 turn {count + 1}",
            }
    state = (
        record.state.value
        if hasattr(record.state, "value") else str(record.state)
    )
    if state == "AWAITING_APPROVAL":
        proposals = list(record.proposals or [])
        seq = str(proposals[-1].seq) if proposals else "?"
        return {
            "actor": "you",
            "text": f"approve or reject proposal #{seq}",
        }
    if state in ("OPEN", "INVESTIGATING"):
        pending = record.pending
        if pending is not None:
            return {
                "actor": "sre",
                "text": f"resumes on the next tick: {pending.trigger}",
            }
        return {"actor": "sre", "text": "investigating"}
    if state == "NEEDS_INPUT":
        reason = record.reason or ""
        paused_at = record.paused_at
        demo_at = None
        for event in record.timeline:
            if event.kind == "demo":
                if paused_at is None or event.at >= paused_at:
                    demo_at = event.at
        transport = getattr(rt, "transport", None)
        exhausted = bool(
            getattr(transport, "exhausted", False)
        ) if transport is not None else False
        if reason == "model_unavailable" and exhausted:
            return {
                "actor": "platform",
                "text": "script ended: switch act to restart",
            }
        if demo_at is not None:
            snapshot = record.paused_snapshot or {}
            tripped = sorted(
                tw for tw, status in snapshot.items()
                if status == "tripped"
            )
            if not tripped:
                tripped = list(record.tripwires or [])
            joined = ", ".join(tripped)
            when = _hhmmss(demo_at)
            return {
                "actor": "tripwire",
                "text": (
                    f"credential rotated at {when}; waiting for "
                    f"{joined} to recover, then the SRE resumes"
                ),
            }
        if reason == "escalated":
            diagnosis = record.diagnosis or {}
            summary = str(diagnosis.get("summary", ""))
            escalation = str(
                (diagnosis.get("action") or {}).get(
                    "escalationSummary", ""
                )
            )
            if _rotatable_faults(rt, record):
                category = str(
                    diagnosis.get("category", "")
                ).replace("_", " ")
                environment = str(
                    record.incident.deployment
                ).split("/", 1)[-1]
                return {
                    "actor": "you",
                    "text": (
                        f"The SRE found {category} on {environment} "
                        f"({summary}). The fix is outside what the "
                        f"SRE may do. {escalation}"
                    ),
                }
            parts = [part for part in (summary, escalation) if part]
            if parts:
                text = " ".join(parts)
            else:
                text = "The SRE escalated; reply or close"
            return {"actor": "you", "text": text}
        if reason in ("budget", "token_budget"):
            return {
                "actor": "you",
                "text": "grant turns, reply or close",
            }
        if reason == "model_unavailable":
            return {
                "actor": "platform",
                "text": "script ended: switch act to restart",
            }
        return {
            "actor": "you",
            "text": "grant turns, reply or close",
        }
    if state == "APPLYING":
        return {
            "actor": "platform", "text": "applying the approved change",
        }
    if state == "VERIFYING":
        ver = _verification_view(rt, record)
        if ver is not None:
            return {
                "actor": "tripwire",
                "text": (
                    f"{ver['tripwire']} must be ok by "
                    f"{_hhmmss(ver['deadline'])}"
                ),
            }
        target = (list(record.tripwires or [""]) or [""])[0]
        return {"actor": "tripwire", "text": f"{target} must be ok"}
    if state in ("RESOLVED", "ESCALATED"):
        closed = ""
        short = ""
        postmortem = record.postmortem
        if (
            postmortem is not None
            and getattr(postmortem, "closed_at", None) is not None
        ):
            closed = postmortem.closed_at.isoformat()
            short = _hhmmss(postmortem.closed_at)
        elif record.timeline:
            at = record.timeline[-1].at
            closed = (
                at.isoformat() if hasattr(at, "isoformat") else str(at)
            )
            short = _hhmmss(at)
        if short:
            text = f"{state} at {short}"
        else:
            text = f"{state} at {closed}"
        return {"actor": "done", "text": text}
    return {"actor": "sre", "text": "investigating"}


def _trip_reason(record: Any) -> str:
    incident = record.incident
    event = getattr(incident, "trip_event", None)
    if event is None:
        return ""
    if isinstance(event, dict):
        return str(event.get("reason", "") or "")
    return str(getattr(event, "reason", "") or "")


def _extract_reply_text(message: str) -> str:
    if not message:
        return ""
    if message.startswith("Reply from ") and ": " in message:
        return message.split(": ", 1)[1]
    return message


def _extract_tripwire_change(message: str) -> str:
    if not message:
        return "the tripwire state changed"
    before = message.split(". Current:")[0]
    if ": " in before:
        return before.rsplit(": ", 1)[-1]
    return before


def _story_entry(
    at: str,
    actor: str,
    kind: str,
    text: str,
    detail: dict[str, Any],
    elapsed: str = "T+0:00",
) -> dict[str, Any]:
    payload = dict(detail)
    return {
        "at": at,
        "actor": actor,
        "kind": kind,
        "text": text,
        "detail": payload,
        "Detail": payload,
        "elapsed": elapsed,
    }


def _elapsed_label(at: Any, trip_at: Any) -> str:
    """Sim time since the trip as T+m:ss for the timeline rail."""
    try:
        seconds = int((at - trip_at).total_seconds())
    except (TypeError, ValueError, AttributeError):
        return "T+0:00"
    if seconds < 0:
        seconds = 0
    return f"T+{seconds // 60}:{seconds % 60:02d}"


def _quote_empty(value: Any) -> str:
    text = str(value)
    return '""' if text == "" else text


def _change_view(proposal: Any) -> dict[str, Any]:
    """A proposal as changed fields only, never raw patch JSON."""
    diagnosis = proposal.diagnosis or {}
    method = str(proposal.method)
    short = method.rsplit(".", 1)[-1]
    rendered = _proposal_view(proposal)
    lines: list[str] = []
    for row in rendered["diff"]:
        field = str(row.get("field", ""))
        if field.startswith("parameters[") and field.endswith("]"):
            label = field[len("parameters["):-1]
        else:
            label = field
        lines.append(
            f"{label}: {_quote_empty(row.get('before', ''))} \u2192 "
            f"{_quote_empty(row.get('after', ''))}"
        )
    if not lines and rendered["steps"]:
        steps = str(rendered["steps"]).replace(": ", " ", 1)
        lines.append(f"{short} ({steps})")
    return {
        "seq": proposal.seq,
        "category": str(diagnosis.get("category", "")),
        "summary": str(diagnosis.get("summary", "")),
        "method": method,
        "short": short,
        "lines": lines,
    }


def story(rt: Any, record: Any) -> list[dict[str, Any]]:
    """Linear incident story: one entry per timeline event (d8-story)."""
    import json as _json

    proposals = list(record.proposals or [])
    by_hash: dict[str, Any] = {}
    for proposal in proposals:
        by_hash[str(getattr(proposal, "hash", ""))] = proposal
    propose_seen = 0
    out: list[dict[str, Any]] = []
    for event in record.timeline:
        kind = str(event.kind)
        detail = dict(event.detail or {})
        at = event.at
        at_text = (
            at.isoformat() if hasattr(at, "isoformat") else str(at)
        )
        if kind == "opened":
            tripwire = str(
                detail.get("tripwire", "")
                or getattr(record.incident, "tripwire", "")
            )
            reason = _trip_reason(record)
            if reason:
                text = f"Tripwire {tripwire} tripped: {reason}"
            else:
                text = f"Tripwire {tripwire} tripped"
            out.append(_story_entry(
                at_text, "tripwire", kind, text, detail
            ))
        elif kind == "attached":
            tripwire = str(detail.get("tripwire", ""))
            text = (
                f"Tripwire {tripwire} tripped and was attached "
                "to this incident"
            )
            out.append(_story_entry(
                at_text, "tripwire", kind, text, detail
            ))
        elif kind == "recovery":
            tripwire = str(detail.get("tripwire", ""))
            out.append(_story_entry(
                at_text, "tripwire", kind,
                f"Tripwire {tripwire} recovered", detail,
            ))
        elif kind == "segment":
            status = str(detail.get("status", ""))
            reason = str(detail.get("reason", ""))
            turns = detail.get("turns", "")
            if status == "submitted" and reason == "propose":
                proposal = (
                    proposals[propose_seen]
                    if propose_seen < len(proposals) else None
                )
                propose_seen += 1
                if proposal is not None:
                    diagnosis = proposal.diagnosis or {}
                    category = str(diagnosis.get("category", ""))
                    summary = str(diagnosis.get("summary", ""))
                    method = str(proposal.method)
                    text = (
                        f"SRE investigated and proposed a fix: "
                        f"{category}: {summary} Proposed {method}."
                    )
                else:
                    text = (
                        f"SRE finished a segment: {status} "
                        f"({reason}) after {turns} turns"
                    )
                out.append(_story_entry(
                    at_text, "sre", kind, text, detail
                ))
            elif status == "submitted" and reason == "none":
                diagnosis = record.diagnosis or {}
                category = str(diagnosis.get("category", ""))
                summary = str(diagnosis.get("summary", ""))
                if category or summary:
                    text = (
                        f"SRE investigated: {category}: {summary} "
                        "No change needed."
                    )
                else:
                    text = (
                        f"SRE finished a segment: {status} "
                        f"({reason}) after {turns} turns"
                    )
                out.append(_story_entry(
                    at_text, "sre", kind, text, detail
                ))
            else:
                if status == "needs_input":
                    text = f"SRE paused the investigation: {reason}"
                else:
                    text = (
                        f"SRE finished a segment: {status} "
                        f"({reason}) after {turns} turns"
                    )
                out.append(_story_entry(
                    at_text, "sre", kind, text, detail
                ))
        elif kind == "proposal":
            seq = int(detail.get("seq", 0) or 0)
            proposal = (
                proposals[seq - 1]
                if 1 <= seq <= len(proposals) else None
            )
            if proposal is not None:
                diagnosis = proposal.diagnosis or {}
                category = str(diagnosis.get("category", ""))
                summary = str(diagnosis.get("summary", ""))
                method = str(proposal.method)
                try:
                    patch_text = _json.dumps(
                        proposal.patch or {}, sort_keys=True
                    )
                except (TypeError, ValueError):
                    patch_text = str(proposal.patch)
                text = (
                    f"SRE proposed #{seq}: {category}: {summary} "
                    f"Proposed {method} with patch {patch_text}."
                )
            else:
                text = f"SRE proposed #{seq}"
            entry = _story_entry(
                at_text, "sre", kind, text, detail
            )
            if proposal is not None:
                entry["change"] = _change_view(proposal)
            out.append(entry)
        elif kind == "needs_input":
            reason = str(
                detail.get("reason", "") or record.reason or ""
            )
            text = f"SRE asked for input: {reason}"
            if reason == "escalated":
                diagnosis = record.diagnosis or {}
                action = diagnosis.get("action") or {}
                summary = action.get("escalationSummary") or ""
                if summary:
                    text = f"{text}. {summary}"
            out.append(_story_entry(
                at_text, "sre", kind, text, detail
            ))
        elif kind == "approved":
            proposal_hash = str(detail.get("proposal_hash", ""))
            proposal = by_hash.get(proposal_hash)
            if proposal is not None:
                text = f"You approved proposal #{proposal.seq}"
            else:
                text = "You approved the proposal"
            out.append(_story_entry(
                at_text, "you", kind, text, detail
            ))
        elif kind == "rejected":
            notes = str(detail.get("notes", ""))
            text = f"You rejected the proposal: {notes}"
            out.append(_story_entry(
                at_text, "you", kind, text, detail
            ))
        elif kind == "closed":
            note = str(detail.get("note", ""))
            if note:
                text = f"You closed the incident: {note}"
            else:
                text = "You closed the incident"
            out.append(_story_entry(
                at_text, "you", kind, text, detail
            ))
        elif kind == "resumed":
            trigger = str(detail.get("trigger", ""))
            out.append(_story_entry(
                at_text, "sre", kind,
                f"SRE resumed: {trigger_words(trigger)}", detail,
            ))
        elif kind == "demo":
            out.append(_story_entry(
                at_text, "you", kind,
                "rotated the credential outside the platform",
                detail,
            ))
        elif kind == "applied":
            out.append(_story_entry(
                at_text, "platform", kind,
                "applied the approved change", detail,
            ))
        elif kind == "verification_started":
            tripwire = str(detail.get("tripwire", ""))
            seconds = detail.get("seconds", 0)
            out.append(_story_entry(
                at_text, "platform", kind,
                f"started verification: {tripwire} must recover "
                f"within {seconds}s",
                detail,
            ))
        elif kind == "verified":
            out.append(_story_entry(
                at_text, "tripwire", kind,
                "verified: the tripwire is ok", detail,
            ))
        elif kind == "verification_failed":
            out.append(_story_entry(
                at_text, "tripwire", kind,
                "verification failed: the tripwire did not recover "
                "in time",
                detail,
            ))
        elif kind == "escalated":
            attempts = detail.get("failed_attempts", "")
            if attempts != "":
                text = (
                    f"escalated after {attempts} failed attempts"
                )
            else:
                text = "escalated"
            out.append(_story_entry(
                at_text, "platform", kind, text, detail
            ))
        elif kind == "conflict":
            resource = str(detail.get("resource", ""))
            out.append(_story_entry(
                at_text, "platform", kind,
                f"proposal conflicted: {resource} changed since "
                "the proposal",
                detail,
            ))
        elif kind == "apply_failed":
            failure = str(detail.get("detail", ""))
            if failure:
                text = f"apply failed: {failure}"
            else:
                text = "apply failed"
            out.append(_story_entry(
                at_text, "platform", kind, text, detail
            ))
        else:
            out.append(_story_entry(
                at_text, "platform", kind, f"{kind}: {detail}",
                detail,
            ))
    trip_at = record.timeline[0].at if record.timeline else None
    for entry, event in zip(out, record.timeline):
        entry["elapsed"] = _elapsed_label(event.at, trip_at)
    opened_index = next(
        (index for index, entry in enumerate(out)
         if entry.get("kind") == "opened"),
        None,
    )
    if opened_index is not None:
        opened = out[opened_index]
        model = display_model(rt)
        out.insert(opened_index + 1, _story_entry(
            str(opened.get("at", "")),
            "sre", "dispatched",
            f"SRE dispatched ({model})",
            {"model": model},
            str(opened.get("elapsed", "T+0:00")),
        ))
    pending = record.pending
    if pending is not None and getattr(pending, "trigger", "") == "reply":
        message = getattr(pending, "message", "") or ""
        reply_text = _extract_reply_text(str(message))
        now = rt.clock.now()
        at_text = (
            now.isoformat() if hasattr(now, "isoformat") else str(now)
        )
        out.append(_story_entry(
            at_text, "you", "reply",
            f"replied: {reply_text} "
            "(queued; the SRE resumes on the next tick)",
            {
                "message": message,
                "trigger": "reply",
                "queued": True,
                "extra_turns": getattr(
                    pending, "extra_turns", 0
                ),
            },
            _elapsed_label(now, trip_at),
        ))
    return out


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
        "actions": handoff_actions(record, rt),
        "next_step": next_step(rt, record),
        "story": story(rt, record),
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
