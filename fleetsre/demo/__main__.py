"""Serve the demo or run --check (d8-pins 7)."""

import argparse
import json
import os
import secrets
import sys
from typing import Any


def _perform(rt: Any, record: Any, action: Any) -> None:
    do = action.do
    if do == "approve":
        latest = record.proposals[-1]
        rt.manager.approve(record.id, latest.hash, by="operator")
    elif do == "reject":
        latest = record.proposals[-1]
        rt.manager.reject(
            record.id, latest.hash, by="operator",
            notes=action.notes or "",
        )
    elif do == "mark_rotated":
        environment = record.incident.deployment.split("/", 1)[-1]
        faults = [
            fault for fault in rt.active_faults()
            if fault["type"] == "credential_expiry"
            and fault["environment"] == environment
        ]
        if faults:
            rt.clear_fault(faults[0]["id"])
    else:
        raise ValueError(f"unknown action: {do}")


def _check_run(name: str, act: Any, delay: int) -> dict[str, Any]:
    from fleetsre.demo.runtime import DemoRuntime

    rt = DemoRuntime(act, live=False)
    rt.act_name = name
    rt.start()
    t0 = act.t0
    tick = act.tick_seconds
    max_steps = int(14400 // tick) + 1
    action_index = 0
    needs_since = None
    first_trip_at: int | None = None
    for _ in range(max_steps):
        records = rt.manager.incidents()
        record = next(
            (item for item in records if item.id == act.incident), None
        )
        if record is not None and first_trip_at is None:
            first_trip_at = int(
                (record.incident.opened_at - t0).total_seconds()
            )
        state = (
            record.state.value
            if record is not None and hasattr(record.state, "value")
            else (str(record.state) if record is not None else "NONE")
        )
        if state in ("RESOLVED", "ESCALATED"):
            break
        if record is not None and action_index < len(act.actions):
            action = act.actions[action_index]
            if state == action.on:
                if action.on == "NEEDS_INPUT":
                    if needs_since is None:
                        needs_since = rt.clock.now()
                    elapsed = (
                        rt.clock.now() - needs_since
                    ).total_seconds()
                    if elapsed >= delay:
                        _perform(rt, record, action)
                        action_index += 1
                        needs_since = None
                        continue
                else:
                    _perform(rt, record, action)
                    action_index += 1
                    continue
            elif state != "NEEDS_INPUT":
                needs_since = None
        before = rt.clock.now()
        rt.advance(tick)
        if rt.clock.now() == before:
            break
    records = rt.manager.incidents()
    record = next(
        (item for item in records if item.id == act.incident), None
    )
    if record is None:
        state = "NONE"
        incident = ""
        refusals = 0
        resolved_at: int | None = None
    else:
        state = (
            record.state.value
            if hasattr(record.state, "value") else str(record.state)
        )
        incident = record.id
        transcript: list[dict[str, Any]] = []
        for tid in record.transcript_ids:
            transcript.extend(rt.store.transcript(tid))
        refusals = sum(
            1 for item in transcript if item.get("verdict") == "refused"
        )
        if state == "RESOLVED" and record.postmortem is not None:
            resolved_at = int(
                (record.postmortem.closed_at - t0).total_seconds()
            )
        else:
            resolved_at = None
        if first_trip_at is None:
            first_trip_at = int(
                (record.incident.opened_at - t0).total_seconds()
            )
    transport = rt.transport
    return {
        "act": name,
        "delay": delay,
        "incident": incident,
        "state": state,
        "modelCalls": rt.model_calls,
        "scriptLength": rt.script_length,
        "exhausted": bool(
            transport.exhausted if transport is not None else False
        ),
        "refusals": refusals,
        "firstTripAt": first_trip_at,
        "resolvedAt": resolved_at,
    }


def _run_check(delays: list[int]) -> int:
    from fleetsre.demo.acts import load_acts

    acts = load_acts()
    ok = True
    for name in sorted(acts):
        act = acts[name]
        for delay in delays:
            run = _check_run(name, act, delay)
            print(f"ACT {json.dumps(run)}", flush=True)
            if not (
                run["state"] == "RESOLVED"
                and run["incident"] == act.incident
                and run["modelCalls"] == run["scriptLength"]
                and run["exhausted"] is False
            ):
                ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """Options: [--act F1] [--host 127.0.0.1] [--port 8000] [--speed 10]
    [--live] [--turn-delay S] or --check [--delay S ...]."""
    parser = argparse.ArgumentParser(
        description="Fleet SRE local demo (d8-pins 7)"
    )
    parser.add_argument("--act", default="F1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--speed", type=float, default=10.0)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--delay", nargs="*", type=int, default=None)
    parser.add_argument("--turn-delay", type=float, default=None)
    args = parser.parse_args(argv)

    if args.check and args.live:
        print(
            "--check refuses --live: CI never spends money",
            file=sys.stderr,
        )
        return 2
    if args.check:
        delays = (
            list(args.delay)
            if args.delay else [0, 600, 1800]
        )
        return _run_check(delays)

    from fleetsre.demo.acts import load_acts
    from fleetsre.demo.host import DemoHost
    from fleetsre.web.app import create_app

    acts = load_acts()
    if args.act not in acts:
        print(f"unknown act: {args.act}", file=sys.stderr)
        return 2
    if args.turn_delay is None:
        turn_delay = 0.0 if args.live else 1.5
    else:
        turn_delay = float(args.turn_delay)
    host = DemoHost(
        acts, live=args.live, speed=args.speed, realtime=True,
        turn_delay_s=turn_delay,
    )
    host.switch(args.act)
    token = os.environ.get("FLEETSRE_ADMIN_TOKEN") or (
        secrets.token_urlsafe(16)
    )
    print(
        f"Admin: http://127.0.0.1:{args.port}/?admin={token}",
        flush=True,
    )
    print(
        f"Act: {args.act} \u2014 {acts[args.act].title}", flush=True
    )
    import uvicorn

    uvicorn.run(
        create_app(host, admin_token=token),
        host="127.0.0.1", port=args.port, log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
