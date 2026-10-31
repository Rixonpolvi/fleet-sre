"""D4 evaluator engine: stateful tripwire evaluation over log entries."""

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from fleetsre.clock import Clock
from fleetsre.contracts import Contract, ErrorSigSpec, SchemaSpec, Tripwire
from fleetsre.evaluator.tripwires import (
    cadence_due,
    canary_missed,
    error_signature,
    schema_violation,
)
from fleetsre.logmodel import LogEntry
from fleetsre.registry import Deployment

_SEV_RANK = {
    "DEBUG": 0,
    "INFO": 1,
    "NOTICE": 2,
    "WARNING": 3,
    "ERROR": 4,
    "CRITICAL": 5,
    "ALERT": 6,
    "EMERGENCY": 7,
}

# Decision E: per (deployment, component) run memory bound.
_RUN_MARGIN = 8


@dataclass(frozen=True)
class TripEvent:
    kind: Literal["trip", "recovery"]
    deployment: str
    tripwire: str
    at: datetime
    reason: str
    evidence: tuple[str, ...]


@dataclass
class _CadenceState:
    armed_at: datetime
    base_armed_at: datetime
    last_ts: datetime | None = None
    last_id: str | None = None
    post_ts: datetime | None = None


@dataclass
class _SchemaState:
    viol_ids: list[str] = field(default_factory=list)
    valid_streak: int = 0


@dataclass
class _RunState:
    worst_rank: int = -1
    worst_name: str = "DEBUG"
    bad_ids: list[str] = field(default_factory=list)


@dataclass
class _ErrorState:
    runs: OrderedDict[str, _RunState] = field(default_factory=OrderedDict)
    retired: set[str] = field(default_factory=set)


@dataclass
class _SyntheticState:
    pending: dict[str, datetime] = field(default_factory=dict)
    seen: dict[str, datetime] = field(default_factory=dict)


@dataclass
class _ChangePoint:
    entry_at: datetime
    expiry: datetime
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


def _resolve(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return params[value[2:-1]]
    return value


def _payload_lookup(payload: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    node: Any = payload
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False, None
    return True, node


class Evaluator:
    def __init__(
        self,
        contracts: list[Contract],
        deployments: list[Deployment],
        clock: Clock,
    ) -> None:
        self._clock = clock
        self._contracts = list(contracts)
        self._by_service = {c.service: c for c in contracts}
        self._deployments = list(deployments)
        self._armed = {d.id: d for d in deployments}
        armed_at = clock.now()

        self._tw_by_comp: dict[tuple[str, str], list[Tripwire]] = {}
        for contract in contracts:
            for tripwire in contract.tripwires:
                key = (contract.service, tripwire.component)
                self._tw_by_comp.setdefault(key, []).append(tripwire)

        # Reverse index for labels.* deploymentBy: (service, component) ->
        # resource instance id -> deployment id.
        self._resource_index: dict[tuple[str, str], dict[str, str]] = {}
        for dep in deployments:
            for comp_id, instance in dep.resources.items():
                key = (dep.service, comp_id)
                self._resource_index.setdefault(key, {})[instance] = dep.id

        self._status: dict[str, dict[str, Literal["ok", "tripped"]]] = {}
        self._cad: dict[tuple[str, str], _CadenceState] = {}
        self._schema: dict[tuple[str, str], _SchemaState] = {}
        self._err: dict[tuple[str, str], _ErrorState] = {}
        self._syn: dict[tuple[str, str], _SyntheticState] = {}
        # Resolved-at-arm-time specs per (deployment, tripwire).
        self._cad_spec: dict[tuple[str, str], tuple[str, int, int]] = {}
        self._schema_spec: dict[tuple[str, str], SchemaSpec] = {}
        self._err_spec: dict[tuple[str, str], ErrorSigSpec] = {}
        self._syn_spec: dict[tuple[str, str], tuple[int, int]] = {}
        for dep in deployments:
            dep_contract = self._by_service.get(dep.service)
            states: dict[str, Literal["ok", "tripped"]] = {}
            self._status[dep.id] = states
            if dep_contract is None:
                continue
            for tripwire in dep_contract.tripwires:
                states[tripwire.id] = "ok"
                key = (dep.id, tripwire.id)
                spec = tripwire.spec
                if tripwire.type == "cadence":
                    self._cad[key] = _CadenceState(armed_at, armed_at)
                    self._cad_spec[key] = (
                        str(spec["event"]),
                        int(_resolve(spec["intervalSeconds"], dep.parameters)),
                        int(_resolve(spec["maxSilentIntervals"], dep.parameters)),
                    )
                elif tripwire.type == "schema":
                    self._schema[key] = _SchemaState()
                    recover = spec.get("recoverAfter")
                    self._schema_spec[key] = SchemaSpec(
                        event=spec.get("event"),
                        field=str(spec["field"]),
                        type=str(spec["type"]),
                        enum=spec.get("enum"),
                        minimum=spec.get("minimum"),
                        required=bool(spec["required"]),
                        consecutive=int(spec.get("consecutive", 1)),
                        recoverAfter=None if recover is None else int(recover),
                        where=spec.get("where"),
                    )
                elif tripwire.type == "errorSignature":
                    self._err[key] = _ErrorState()
                    self._err_spec[key] = ErrorSigSpec(
                        minSeverity=str(spec["minSeverity"]),
                        windowRuns=int(
                            _resolve(spec["windowRuns"], dep.parameters)
                        ),
                        threshold=int(
                            _resolve(spec["threshold"], dep.parameters)
                        ),
                    )
                elif tripwire.type == "synthetic":
                    self._syn[key] = _SyntheticState()
                    self._syn_spec[key] = (
                        int(_resolve(spec["everyMinutes"], dep.parameters)),
                        int(_resolve(spec["deadlineSeconds"], dep.parameters)),
                    )
        self._change_points: dict[tuple[str, str], _ChangePoint] = {}
        # A run's severity is final when it completes. Judge errorSignature
        # runs on completion entries: the component's cadence heartbeat
        # event when it has one, else every entry.
        self._completion_event: dict[tuple[str, str], str] = {}
        for contract in contracts:
            for tripwire in contract.tripwires:
                if tripwire.type == "cadence":
                    key = (contract.service, tripwire.component)
                    self._completion_event.setdefault(
                        key, str(tripwire.spec["event"])
                    )

    @property
    def deployments(self) -> tuple[Deployment, ...]:
        return tuple(self._deployments)

    def _override(
        self, dep_id: str, component: str, tripwire: str, field_name: str
    ) -> Any:
        cp = self._change_points.get((dep_id, component))
        if cp is None:
            return None
        fields = cp.overrides.get(tripwire)
        if fields is None:
            return None
        return fields.get(field_name, None)

    def _eff_cadence(self, dep_id: str, tripwire: Tripwire) -> tuple[str, int, int]:
        event, interval, max_silent = self._cad_spec[(dep_id, tripwire.id)]
        params = self._armed[dep_id].parameters
        found = self._override(dep_id, tripwire.component, tripwire.id, "event")
        if found is not None:
            event = str(_resolve(found, params))
        found = self._override(
            dep_id, tripwire.component, tripwire.id, "intervalSeconds"
        )
        if found is not None:
            interval = int(_resolve(found, params))
        found = self._override(
            dep_id, tripwire.component, tripwire.id, "maxSilentIntervals"
        )
        if found is not None:
            max_silent = int(_resolve(found, params))
        return event, interval, max_silent

    def _eff_schema_counts(self, dep_id: str, tripwire: Tripwire) -> tuple[int, int]:
        base = self._schema_spec[(dep_id, tripwire.id)]
        params = self._armed[dep_id].parameters
        consecutive = base.consecutive
        recover_after = (
            base.recoverAfter if base.recoverAfter is not None else consecutive
        )
        found = self._override(
            dep_id, tripwire.component, tripwire.id, "consecutive"
        )
        if found is not None:
            consecutive = int(_resolve(found, params))
        found = self._override(
            dep_id, tripwire.component, tripwire.id, "recoverAfter"
        )
        if found is not None:
            recover_after = int(_resolve(found, params))
        return consecutive, recover_after

    def _eff_error(
        self, dep_id: str, tripwire: Tripwire
    ) -> tuple[str, int, int]:
        base = self._err_spec[(dep_id, tripwire.id)]
        params = self._armed[dep_id].parameters
        min_sev = base.minSeverity
        window = base.windowRuns
        threshold = base.threshold
        found = self._override(
            dep_id, tripwire.component, tripwire.id, "minSeverity"
        )
        if found is not None:
            min_sev = str(_resolve(found, params))
        found = self._override(
            dep_id, tripwire.component, tripwire.id, "windowRuns"
        )
        if found is not None:
            window = int(_resolve(found, params))
        found = self._override(
            dep_id, tripwire.component, tripwire.id, "threshold"
        )
        if found is not None:
            threshold = int(_resolve(found, params))
        return min_sev, window, threshold

    def _eff_deadline(self, dep_id: str, tripwire: Tripwire) -> int:
        _every, deadline = self._syn_spec[(dep_id, tripwire.id)]
        params = self._armed[dep_id].parameters
        found = self._override(
            dep_id, tripwire.component, tripwire.id, "deadlineSeconds"
        )
        if found is not None:
            deadline = int(_resolve(found, params))
        return deadline

    def _resolve_deployment(
        self, service: str, component_id: str, deployment_by: str, entry: LogEntry
    ) -> str | None:
        if deployment_by.startswith("labels."):
            label_key = deployment_by[len("labels.") :]
            index = self._resource_index.get((service, component_id), {})
            if not index:
                env = entry.jsonPayload.get("environment")
                if not isinstance(env, str):
                    return None
                return f"{service}/{env}"
            value = entry.labels.get(label_key)
            if value is None:
                return None
            return index.get(value)
        if deployment_by.startswith("jsonPayload."):
            node: Any = entry.jsonPayload
            for part in deployment_by.split(".")[1:]:
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return None
            if not isinstance(node, str):
                return None
            return f"{service}/{node}"
        return None

    def ingest(self, entry: LogEntry) -> list[TripEvent]:
        events: list[TripEvent] = []
        container = entry.resource.labels.get("container_name")
        for contract in self._contracts:
            for component in contract.components:
                if component.logs.container_name != container:
                    continue
                labels = entry.labels
                if any(key not in labels for key in component.logs.match):
                    continue
                dep_id = self._resolve_deployment(
                    contract.service,
                    component.id,
                    component.logs.deploymentBy,
                    entry,
                )
                if dep_id is None:
                    continue
                dep = self._armed.get(dep_id)
                if dep is None or dep.service != contract.service:
                    continue
                tripwires = self._tw_by_comp.get(
                    (contract.service, component.id), []
                )
                for tripwire in tripwires:
                    self._ingest_one(
                        contract, dep_id, tripwire, entry, events
                    )
        return events

    def _ingest_one(
        self,
        contract: Contract,
        dep_id: str,
        tripwire: Tripwire,
        entry: LogEntry,
        events: list[TripEvent],
    ) -> None:
        if tripwire.type == "cadence":
            self._ingest_cadence(dep_id, tripwire, entry, events)
        elif tripwire.type == "schema":
            self._ingest_schema(dep_id, tripwire, entry, events)
        elif tripwire.type == "errorSignature":
            self._ingest_error(contract, dep_id, tripwire, entry, events)
        elif tripwire.type == "synthetic":
            self._ingest_synthetic(dep_id, tripwire, entry, events)

    def _ingest_cadence(
        self,
        dep_id: str,
        tripwire: Tripwire,
        entry: LogEntry,
        events: list[TripEvent],
    ) -> None:
        event, interval, max_silent = self._eff_cadence(dep_id, tripwire)
        if entry.jsonPayload.get("event") != event:
            return
        state = self._cad[(dep_id, tripwire.id)]
        ts = entry.timestamp
        if state.last_ts is None or ts > state.last_ts:
            state.last_ts = ts
            state.last_id = entry.insertId
        if ts >= state.armed_at and (
            state.post_ts is None or ts > state.post_ts
        ):
            state.post_ts = ts
        if self._status[dep_id][tripwire.id] == "tripped":
            if cadence_due(
                state.post_ts,
                state.armed_at,
                self._clock.now(),
                interval,
                max_silent,
            ):
                return
            self._status[dep_id][tripwire.id] = "ok"
            events.append(
                TripEvent(
                    kind="recovery",
                    deployment=dep_id,
                    tripwire=tripwire.id,
                    at=self._clock.now(),
                    reason=f"heartbeat {event} seen again",
                    evidence=(entry.insertId,),
                )
            )

    def _ingest_schema(
        self,
        dep_id: str,
        tripwire: Tripwire,
        entry: LogEntry,
        events: list[TripEvent],
    ) -> None:
        spec = self._schema_spec[(dep_id, tripwire.id)]
        if spec.event is not None:
            if entry.jsonPayload.get("event") != spec.event:
                return
        if spec.where:
            for key, want in spec.where.items():
                found, value = _payload_lookup(
                    entry.jsonPayload, key.removeprefix("jsonPayload.")
                )
                if not found or value != want:
                    return
        consecutive, recover_after = self._eff_schema_counts(dep_id, tripwire)
        state = self._schema[(dep_id, tripwire.id)]
        reason = schema_violation(entry, spec)
        if reason is not None:
            state.valid_streak = 0
            state.viol_ids.append(entry.insertId)
            if len(state.viol_ids) > consecutive:
                del state.viol_ids[: len(state.viol_ids) - consecutive]
            if (
                self._status[dep_id][tripwire.id] == "ok"
                and len(state.viol_ids) >= consecutive
            ):
                self._status[dep_id][tripwire.id] = "tripped"
                events.append(
                    TripEvent(
                        kind="trip",
                        deployment=dep_id,
                        tripwire=tripwire.id,
                        at=self._clock.now(),
                        reason=reason,
                        evidence=tuple(state.viol_ids[-consecutive:]),
                    )
                )
        else:
            state.viol_ids.clear()
            if self._status[dep_id][tripwire.id] == "tripped":
                state.valid_streak += 1
                if state.valid_streak >= recover_after:
                    state.valid_streak = 0
                    self._status[dep_id][tripwire.id] = "ok"
                    events.append(
                        TripEvent(
                            kind="recovery",
                            deployment=dep_id,
                            tripwire=tripwire.id,
                            at=self._clock.now(),
                            reason=(
                                f"{recover_after} consecutive valid entries"
                            ),
                            evidence=(entry.insertId,),
                        )
                    )

    def _ingest_error(
        self,
        contract: Contract,
        dep_id: str,
        tripwire: Tripwire,
        entry: LogEntry,
        events: list[TripEvent],
    ) -> None:
        run_id = entry.jsonPayload.get("runId")
        if not isinstance(run_id, str):
            return
        min_sev, window, threshold = self._eff_error(dep_id, tripwire)
        min_rank = _SEV_RANK.get(min_sev, 0)
        state = self._err[(dep_id, tripwire.id)]
        rank = _SEV_RANK.get(entry.severity, -1)
        run = state.runs.get(run_id)
        if run is None:
            if run_id in state.retired:
                return
            bound = window + _RUN_MARGIN
            while len(state.runs) >= bound:
                old_id, _old = state.runs.popitem(last=False)
                state.retired.add(old_id)
            run = _RunState()
            state.runs[run_id] = run
        if rank > run.worst_rank:
            run.worst_rank = rank
            run.worst_name = entry.severity
        if rank >= min_rank:
            run.bad_ids.append(entry.insertId)
        completion = self._completion_event.get(
            (contract.service, tripwire.component)
        )
        if (
            completion is not None
            and entry.jsonPayload.get("event") != completion
        ):
            return
        recent = [r.worst_name for r in state.runs.values()]
        tripped = error_signature(
            recent, ErrorSigSpec(
                minSeverity=min_sev, windowRuns=window, threshold=threshold
            ),
        )
        if tripped and self._status[dep_id][tripwire.id] == "ok":
            self._status[dep_id][tripwire.id] = "tripped"
            evidence: list[str] = []
            for window_run in list(state.runs.values())[-window:]:
                evidence.extend(window_run.bad_ids)
            events.append(
                TripEvent(
                    kind="trip",
                    deployment=dep_id,
                    tripwire=tripwire.id,
                    at=self._clock.now(),
                    reason=(
                        f"{threshold} of last {window} runs at "
                        f"{min_sev} or worse"
                    ),
                    evidence=tuple(evidence),
                )
            )
        elif self._status[dep_id][tripwire.id] == "tripped":
            # Recovery needs window-threshold+1 consecutive clean runs: the
            # smallest streak that proves the burst ended (and guarantees
            # the window is below threshold). Judging the raw window would
            # flap trip/recovery while the rate hovers at the threshold.
            need = window - threshold + 1
            streak = 0
            for window_run in reversed(state.runs.values()):
                if window_run.worst_rank < min_rank:
                    streak += 1
                else:
                    break
            if streak >= need:
                self._status[dep_id][tripwire.id] = "ok"
                events.append(
                    TripEvent(
                        kind="recovery",
                        deployment=dep_id,
                        tripwire=tripwire.id,
                        at=self._clock.now(),
                        reason=f"{need} consecutive clean runs",
                        evidence=(entry.insertId,),
                    )
                )

    def _ingest_synthetic(
        self,
        dep_id: str,
        tripwire: Tripwire,
        entry: LogEntry,
        events: list[TripEvent],
    ) -> None:
        if entry.jsonPayload.get("event") != "alert.ingested":
            return
        alert = entry.jsonPayload.get("alert")
        if not isinstance(alert, dict) or alert.get("canary") is not True:
            return
        ticket = alert.get("ticket_id")
        if not isinstance(ticket, str):
            return
        state = self._syn[(dep_id, tripwire.id)]
        planted_at = state.pending.get(ticket)
        if planted_at is None:
            return
        deadline = self._eff_deadline(dep_id, tripwire)
        if (entry.timestamp - planted_at).total_seconds() <= deadline:
            del state.pending[ticket]
            state.seen.pop(ticket, None)
            if self._status[dep_id][tripwire.id] == "tripped":
                self._status[dep_id][tripwire.id] = "ok"
                events.append(
                    TripEvent(
                        kind="recovery",
                        deployment=dep_id,
                        tripwire=tripwire.id,
                        at=self._clock.now(),
                        reason=f"canary {ticket} seen within deadline",
                        evidence=(entry.insertId,),
                    )
                )
        else:
            state.seen[ticket] = entry.timestamp

    def tick(self) -> list[TripEvent]:
        now = self._clock.now()
        expired = [
            key for key, cp in self._change_points.items() if now >= cp.expiry
        ]
        for key in expired:
            del self._change_points[key]
            dep_id, component = key
            dep = self._armed.get(dep_id)
            if dep is None:
                continue
            contract = self._by_service.get(dep.service)
            if contract is None:
                continue
            for tripwire in contract.tripwires:
                if (
                    tripwire.component != component
                    or tripwire.type != "cadence"
                ):
                    continue
                state = self._cad.get((dep_id, tripwire.id))
                if state is None:
                    continue
                state.armed_at = state.base_armed_at
                if (
                    state.last_ts is not None
                    and state.last_ts >= state.armed_at
                ):
                    state.post_ts = state.last_ts
                else:
                    state.post_ts = None
        events: list[TripEvent] = []
        for dep_id in sorted(self._status):
            dep = self._armed.get(dep_id)
            if dep is None:
                continue
            contract = self._by_service.get(dep.service)
            if contract is None:
                continue
            for tripwire in contract.tripwires:
                if tripwire.type == "cadence":
                    self._tick_cadence(dep_id, tripwire, now, events)
                elif tripwire.type == "synthetic":
                    self._tick_synthetic(dep_id, tripwire, now, events)
        return events

    def _tick_cadence(
        self,
        dep_id: str,
        tripwire: Tripwire,
        now: datetime,
        events: list[TripEvent],
    ) -> None:
        if self._status[dep_id][tripwire.id] != "ok":
            return
        event, interval, max_silent = self._eff_cadence(dep_id, tripwire)
        state = self._cad[(dep_id, tripwire.id)]
        if cadence_due(state.post_ts, state.armed_at, now, interval, max_silent):
            self._status[dep_id][tripwire.id] = "tripped"
            evidence = (state.last_id,) if state.last_id is not None else ()
            events.append(
                TripEvent(
                    kind="trip",
                    deployment=dep_id,
                    tripwire=tripwire.id,
                    at=now,
                    reason=(
                        f"no {event} for more than "
                        f"{max_silent} x {interval}s"
                    ),
                    evidence=evidence,
                )
            )

    def _tick_synthetic(
        self,
        dep_id: str,
        tripwire: Tripwire,
        now: datetime,
        events: list[TripEvent],
    ) -> None:
        state = self._syn[(dep_id, tripwire.id)]
        if not state.pending:
            return
        deadline = self._eff_deadline(dep_id, tripwire)
        missed = [
            pid
            for pid, planted_at in state.pending.items()
            if canary_missed(planted_at, state.seen.get(pid), now, deadline)
        ]
        for pid in missed:
            del state.pending[pid]
            state.seen.pop(pid, None)
        if missed and self._status[dep_id][tripwire.id] == "ok":
            self._status[dep_id][tripwire.id] = "tripped"
            events.append(
                TripEvent(
                    kind="trip",
                    deployment=dep_id,
                    tripwire=tripwire.id,
                    at=now,
                    reason=(
                        "canary missed deadline "
                        f"({deadline}s): {', '.join(sorted(missed))}"
                    ),
                    evidence=(),
                )
            )

    def canary_planted(
        self, deployment: str, provider_incident_id: str, at: datetime
    ) -> None:
        dep = self._armed.get(deployment)
        if dep is None:
            return
        contract = self._by_service.get(dep.service)
        if contract is None:
            return
        for tripwire in contract.tripwires:
            if tripwire.type != "synthetic":
                continue
            state = self._syn.get((deployment, tripwire.id))
            if state is None:
                continue
            state.pending[provider_incident_id] = at
            state.seen.pop(provider_incident_id, None)

    def enter_change_point(
        self,
        deployment: str,
        component: str,
        *,
        duration_s: int,
        overrides: dict[str, dict[str, Any]],
    ) -> None:
        dep = self._armed.get(deployment)
        if dep is None:
            return
        contract = self._by_service.get(dep.service)
        if contract is None:
            return
        now = self._clock.now()
        key = (deployment, component)
        existing = self._change_points.get(key)
        scoped = {
            tw_id: dict(fields)
            for tw_id, fields in overrides.items()
            if any(
                t.id == tw_id and t.component == component
                for t in contract.tripwires
            )
        }
        if existing is None:
            self._change_points[key] = _ChangePoint(
                entry_at=now,
                expiry=now + timedelta(seconds=duration_s),
                overrides=scoped,
            )
        else:
            existing.entry_at = now
            existing.expiry = now + timedelta(seconds=duration_s)
            existing.overrides = scoped
        for tripwire in contract.tripwires:
            if tripwire.component != component or tripwire.type != "cadence":
                continue
            state = self._cad.get((deployment, tripwire.id))
            if state is None:
                continue
            state.armed_at = now
            state.post_ts = None

    def status(self) -> dict[str, dict[str, Literal["ok", "tripped"]]]:
        return {
            dep_id: dict(states) for dep_id, states in self._status.items()
        }
