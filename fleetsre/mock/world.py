"""Mock world: deterministic one-process SecOps simulation (mocks.md §8).

Scheduler: a heap of (time, sequence). Worker runs sit on a fixed grid from
the start time; runOnDemand executes immediately without moving the grid.
"""

# invented: every jsonPayload field, event names, log record messages, the
# fault mechanics, and any resource value the docs do not define.

import heapq
import itertools
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from pydantic import BaseModel

from fleetsre.clock import Clock
from fleetsre.logmodel import LogEntry, MonitoredResource
from fleetsre.mock.fleet import FleetConfig
from fleetsre.mock.ingestion import incident_to_alert
from fleetsre.mock.jobs import fetch_indicators
from fleetsre.mock.sentinel import FakeSentinel, seeded_rng
from fleetsre.ports import LogSink

FAULT_TYPES = frozenset(
    {
        "connector_stall",
        "credential_expiry",
        "severity_drift",
        "job_hollow_success",
        "job_error",
        "connector_flap",
        "job_stall",
    }
)

CONNECTOR_PATCHABLE = frozenset(
    {"enabled", "intervalSeconds", "parameters", "displayName", "description"}
)

JOB_PATCHABLE = frozenset(
    {"enabled", "intervalSeconds", "parameters", "displayName"}
)

_CRED_MESSAGE = "Failed to authenticate to Sentinel workspace: 401 Unauthorized"
_FEED_MESSAGE = "Feed returned 500 Internal Server Error"
_FLAP_MESSAGE = "Transient connector failure during run"


class ApiError(Exception):
    """Google-shaped API error: code, status and message."""

    def __init__(self, code: int, status: str, message: str):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


class _SteppableClock(Protocol):
    def now(self) -> datetime: ...
    def set(self, t: datetime) -> None: ...


class FaultSpec(BaseModel):
    type: str
    environment: str
    target: str | None = None
    params: dict[str, Any] = {}


@dataclass
class _Fault:
    id: str
    spec: FaultSpec
    replaced: Any = None


@dataclass
class _Environment:
    name: str
    id: str
    display_name: str
    description: str
    contact_emails: list[str]


@dataclass
class _IntegrationInstance:
    name: str
    env: str
    integration: str
    instance_id: str
    display_name: str
    params: list[dict[str, Any]]


@dataclass
class _Connector:
    name: str
    env: str
    integration: str
    connector: str
    instance_id: str
    display_name: str
    product_field: str
    event_field: str
    interval_seconds: int
    enabled: bool
    version: int
    integration_version: str
    custom: bool
    params: list[dict[str, Any]]
    logging_until_ms: int = 0
    fetched_total: int = 0


@dataclass
class _Job:
    name: str
    env: str
    integration: str
    job: str
    instance_id: str
    display_name: str
    interval_seconds: int
    enabled: bool
    params: list[dict[str, Any]]
    create_time: datetime
    update_time: datetime
    last_run_time: datetime | None = None
    last_run_status: str = "SUCCESS"
    next_scheduled: datetime | None = None


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _slug(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in text).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "resource"


class World:
    def __init__(
        self, fleet: FleetConfig, seed: int, clock: Clock, sink: LogSink
    ) -> None:
        self.fleet = fleet
        self.seed = seed
        self.clock: _SteppableClock = cast(_SteppableClock, clock)
        self.sink = sink
        self.prefix = fleet.instance
        self._start = clock.now()
        self._heap: list[tuple[datetime, int, str, Any]] = []
        self._seq = itertools.count()
        self._run_seq = 0
        self._insert_seq = 0
        self._log_seq = 0
        self._fault_seq = 0
        self._sched_seq = 0
        self._connector_rngs: dict[str, random.Random] = {}
        self._job_rngs: dict[str, random.Random] = {}
        self._ids_insert = seeded_rng(seed, "ids:insert")
        self._ids_corr = seeded_rng(seed, "ids:correlation")
        self._ids_canary = seeded_rng(seed, "ids:canary")
        self._envs: dict[str, _Environment] = {}
        self._integrations: dict[str, _IntegrationInstance] = {}
        self._connectors: dict[str, _Connector] = {}
        self._jobs: dict[str, _Job] = {}
        self._connector_logs: dict[str, list[dict[str, Any]]] = {}
        self._job_logs: dict[str, list[dict[str, Any]]] = {}
        self._faults: dict[str, _Fault] = {}
        self._seen_faults: set[str] = set()
        self._sched_faults: dict[int, str] = {}
        rates: dict[str, float] = {}
        for env_id, env in fleet.environments.items():
            dep = env.deployments.get("alert-ingestion")
            if dep is not None:
                rates[env_id] = float(dep.parameters.get("expectedAlertsPerHour", 0))
        self._upstream = FakeSentinel(seed, self._start, rates)
        self._build_resources()
        for name in self._connectors:
            st = self._connectors[name]
            self._push(self._start + timedelta(seconds=st.interval_seconds),
                       "connector", name)
        for name in self._jobs:
            job = self._jobs[name]
            job.next_scheduled = self._start + timedelta(
                seconds=job.interval_seconds
            )
            self._push(job.next_scheduled, "job", name)

    # -- scheduler ------------------------------------------------------

    def _push(self, at: datetime, kind: str, payload: Any) -> None:
        heapq.heappush(self._heap, (at, next(self._seq), kind, payload))

    def step_until(self, t: datetime) -> None:
        if t < self.clock.now():
            return
        while self._heap and self._heap[0][0] <= t:
            at, _, kind, payload = heapq.heappop(self._heap)
            self.clock.set(at)
            if kind == "connector":
                self._run_connector(at, payload)
                st = self._connectors.get(payload)
                if st is not None:
                    self._push(
                        at + timedelta(seconds=st.interval_seconds),
                        "connector",
                        payload,
                    )
            elif kind == "job":
                self._run_job(at, payload)
                stj = self._jobs.get(payload)
                if stj is not None:
                    self._push(
                        at + timedelta(seconds=stj.interval_seconds), "job", payload
                    )
            elif kind == "fault_on":
                self._fire_scheduled(payload)
            elif kind == "fault_off":
                fid = payload
                if fid in self._faults:
                    self.clear(fid)
        self.clock.set(t)

    # -- resources ------------------------------------------------------

    def _integration_with(self, section: str) -> tuple[str, dict[str, Any]]:
        for name, spec in self.fleet.integrations.items():
            if isinstance(spec, dict) and section in spec:
                return name, spec
        raise ApiError(500, "INTERNAL", f"no integration with {section}")

    def _build_resources(self) -> None:
        sentinel, sentinel_spec = self._integration_with("connectors")
        intel, intel_spec = self._integration_with("jobs")
        conn_key = next(iter(sentinel_spec["connectors"]))
        conn_def = sentinel_spec["connectors"][conn_key]
        job_key = next(iter(intel_spec["jobs"]))
        job_def = intel_spec["jobs"][job_key]
        for env_id, env in self.fleet.environments.items():
            self._envs[env_id] = _Environment(
                name=f"{self.prefix}/environments/{env_id}",
                id=env_id,
                display_name=env.displayName,
                description=env.description,
                contact_emails=list(env.contactEmails),
            )
            dep = env.deployments.get("alert-ingestion")
            if dep is not None and dep.connectorInstance:
                self._add_connector(
                    sentinel, conn_key, conn_def, env_id, dep.connectorInstance,
                    int(dep.parameters.get("intervalSeconds", 60)),
                    sentinel_spec,
                )
            depj = env.deployments.get("intel-sync")
            if depj is not None and depj.jobInstance:
                self._add_job(
                    intel, job_key, job_def, env_id, depj.jobInstance,
                    int(depj.parameters.get("intervalSeconds", 300)),
                )
        for env_id in self.fleet.environments:
            if "alert-ingestion" in self.fleet.environments[env_id].deployments:
                self._add_integration_instance(sentinel, conn_def, env_id)
            if "intel-sync" in self.fleet.environments[env_id].deployments:
                self._add_job_integration_instance(intel, job_def, env_id)

    def _credential_params(
        self, conn_def: dict[str, Any], env_id: str
    ) -> list[dict[str, Any]]:
        creds = self.fleet.environments[env_id].credentials
        params = []
        for spec in conn_def.get("parameters", []):
            if "fromCredential" in spec:
                value = creds.get(spec["fromCredential"], "")
            else:
                value = str(spec.get("default", "")).replace("{env}", env_id)
            params.append(
                {
                    "value": value,
                    "displayName": spec.get("displayName", ""),
                    "description": "",
                    "mandatory": bool(spec.get("mandatory", False)),
                    "advanced": False,
                    "type": spec.get("type", "STRING"),
                }
            )
        return params

    def _add_connector(
        self,
        integration: str,
        conn_key: str,
        conn_def: dict[str, Any],
        env_id: str,
        instance_id: str,
        interval: int,
        spec: dict[str, Any],
    ) -> _Connector:
        name = (
            f"{self.prefix}/integrations/{integration}/connectors/"
            f"{conn_key}/connectorInstances/{instance_id}"
        )
        st = _Connector(
            name=name,
            env=env_id,
            integration=integration,
            connector=conn_key,
            instance_id=instance_id,
            display_name=str(conn_def.get("displayName", "")),
            product_field=str(conn_def.get("productFieldName", "")),
            event_field=str(conn_def.get("eventFieldName", "")),
            interval_seconds=interval,
            enabled=True,
            version=1,
            integration_version=str(spec.get("version", "1.0")),
            custom=bool(spec.get("custom", True)),
            params=self._credential_params(conn_def, env_id),
        )
        self._connectors[name] = st
        self._connector_logs[name] = []
        return st

    def _job_params(self, job_def: dict[str, Any], env_id: str) -> list[dict[str, Any]]:
        params = []
        for i, spec in enumerate(job_def.get("parameters", [])):
            value = str(spec.get("default", "")).replace("{env}", env_id)
            params.append(
                {
                    "id": f"param-{i}",
                    "mandatory": bool(spec.get("mandatory", False)),
                    "value": value,
                    "displayName": spec.get("displayName", ""),
                    "type": spec.get("type", "STRING"),
                }
            )
        return params

    def _add_job(
        self,
        integration: str,
        job_key: str,
        job_def: dict[str, Any],
        env_id: str,
        instance_id: str,
        interval: int,
    ) -> _Job:
        name = (
            f"{self.prefix}/integrations/{integration}/jobs/"
            f"{job_key}/jobInstances/{instance_id}"
        )
        st = _Job(
            name=name,
            env=env_id,
            integration=integration,
            job=job_key,
            instance_id=instance_id,
            display_name=str(job_def.get("displayName", "")),
            interval_seconds=interval,
            enabled=True,
            params=self._job_params(job_def, env_id),
            create_time=self._start,
            update_time=self._start,
        )
        self._jobs[name] = st
        self._job_logs[name] = []
        return st

    def _add_integration_instance(
        self, integration: str, conn_def: dict[str, Any], env_id: str
    ) -> None:
        name = (
            f"{self.prefix}/integrations/{integration}/"
            f"integrationInstances/{env_id}"
        )
        creds = self.fleet.environments[env_id].credentials
        params = []
        i = 0
        for spec in conn_def.get("parameters", []):
            if "fromCredential" not in spec:
                continue
            params.append(
                {
                    "id": f"param-{i}",
                    "displayName": spec.get("displayName", ""),
                    "type": spec.get("type", "STRING"),
                    "description": "",
                    "propertyName": _slug(spec.get("displayName", "")),
                    "mandatory": bool(spec.get("mandatory", False)),
                    "value": creds.get(spec["fromCredential"], ""),
                }
            )
            i += 1
        self._integrations[name] = _IntegrationInstance(
            name=name,
            env=env_id,
            integration=integration,
            instance_id=env_id,
            display_name=f"{integration} {env_id}",
            params=params,
        )

    def _add_job_integration_instance(
        self, integration: str, job_def: dict[str, Any], env_id: str
    ) -> None:
        name = (
            f"{self.prefix}/integrations/{integration}/"
            f"integrationInstances/{env_id}"
        )
        params = []
        for i, spec in enumerate(job_def.get("parameters", [])):
            params.append(
                {
                    "id": f"param-{i}",
                    "displayName": spec.get("displayName", ""),
                    "type": spec.get("type", "STRING"),
                    "description": "",
                    "propertyName": _slug(spec.get("displayName", "")),
                    "mandatory": bool(spec.get("mandatory", False)),
                    "value": str(spec.get("default", "")).replace("{env}", env_id),
                }
            )
        self._integrations[name] = _IntegrationInstance(
            name=name,
            env=env_id,
            integration=integration,
            instance_id=env_id,
            display_name=f"{integration} {env_id}",
            params=params,
        )

    # -- faults ---------------------------------------------------------

    def inject(self, fault: FaultSpec) -> str:
        if fault.type not in FAULT_TYPES:
            raise ApiError(400, "INVALID_ARGUMENT",
                           f"unknown fault type {fault.type!r}")
        if fault.environment not in self.fleet.environments:
            raise ApiError(404, "NOT_FOUND",
                           f"unknown environment {fault.environment!r}")
        self._fault_seq += 1
        fid = f"fault-{self._fault_seq}"
        replaced = None
        if fault.type == "job_hollow_success":
            replaced = self._job_param(fault.environment, "Lookback Hours")
            self._set_job_param(fault.environment, "Lookback Hours", "0")
        self._faults[fid] = _Fault(id=fid, spec=fault, replaced=replaced)
        self._seen_faults.add(fid)
        return fid

    def clear(self, fault_id: str) -> None:
        fault = self._faults.pop(fault_id, None)
        if fault is None:
            if fault_id in self._seen_faults:
                return
            raise ApiError(404, "NOT_FOUND", f"unknown fault {fault_id!r}")
        if fault.spec.type == "job_hollow_success" and fault.replaced is not None:
            self._set_job_param(
                fault.spec.environment, "Lookback Hours", fault.replaced
            )

    def list_faults(self) -> list[dict[str, Any]]:
        return [
            {
                "id": fid,
                "type": f.spec.type,
                "environment": f.spec.environment,
                "target": f.spec.target,
                "params": f.spec.params,
            }
            for fid, f in sorted(self._faults.items())
        ]

    def schedule(self, items: list[dict[str, Any]]) -> int:
        """Queue chaos items; `at`/`clearAt` are offsets from world start."""
        count = 0
        for item in items:
            self._sched_seq += 1
            sid = self._sched_seq
            fire_at = self._start + timedelta(seconds=float(item.get("at", 0)))
            clear_at = item.get("clearAt")
            payload = {
                "sched_id": sid,
                "type": item.get("type", ""),
                "environment": item.get("environment", ""),
                "target": item.get("target"),
                "params": dict(item.get("params", {}) or {}),
                "clear_at": (
                    self._start + timedelta(seconds=float(clear_at))
                    if clear_at is not None
                    else None
                ),
            }
            if fire_at <= self.clock.now():
                self._fire_scheduled(payload)
            else:
                self._push(fire_at, "fault_on", payload)
            count += 1
        return count

    def _fire_scheduled(self, payload: dict[str, Any]) -> None:
        fid = self.inject(
            FaultSpec(
                type=payload["type"],
                environment=payload["environment"],
                target=payload.get("target"),
                params=payload.get("params", {}),
            )
        )
        self._sched_faults[payload["sched_id"]] = fid
        clear_at = payload.get("clear_at")
        if clear_at is not None:
            if clear_at <= self.clock.now():
                self.clear(fid)
            else:
                self._push(clear_at, "fault_off", fid)

    def _active(self, env: str, ftype: str) -> list[_Fault]:
        return [
            f
            for f in self._faults.values()
            if f.spec.environment == env and f.spec.type == ftype
        ]

    def _rate(self, env: str, ftype: str) -> float:
        faults = self._active(env, ftype)
        if not faults:
            return 0.0
        return float(faults[0].spec.params.get("rate", 1.0))

    def credential_ok(self, env: str) -> bool:
        """Shared credential check: the token call and executeTest both use it."""
        return not self._active(env, "credential_expiry")

    def _clear_stalls(self, env: str, ftype: str) -> None:
        for fid in [fid for fid, f in self._faults.items()
                    if f.spec.environment == env and f.spec.type == ftype]:
            self.clear(fid)

    # -- canary ---------------------------------------------------------

    def plant_canary(self, environment: str) -> str:
        if not self._upstream.has(environment):
            raise ApiError(404, "NOT_FOUND",
                           f"unknown environment {environment!r}")
        provider_id = f"canary-{self._ids_canary.getrandbits(64):016x}"
        self._upstream.plant_canary(
            environment, provider_id, self.clock.now()
        )
        return provider_id

    # -- workers --------------------------------------------------------

    def _connector_rng(self, instance_id: str) -> random.Random:
        rng = self._connector_rngs.get(instance_id)
        if rng is None:
            rng = seeded_rng(self.seed, f"worker:connector:{instance_id}")
            self._connector_rngs[instance_id] = rng
        return rng

    def _job_rng(self, instance_id: str) -> random.Random:
        rng = self._job_rngs.get(instance_id)
        if rng is None:
            rng = seeded_rng(self.seed, f"worker:job:{instance_id}")
            self._job_rngs[instance_id] = rng
        return rng

    def _next_run_id(self) -> str:
        self._run_seq += 1
        return f"run-{self._run_seq:08d}"

    def _correlation_id(self) -> str:
        h = f"{self._ids_corr.getrandbits(128):032x}"
        return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-8{h[17:20]}-{h[20:]}"

    def _write(
        self,
        at: datetime,
        severity: str,
        container: str,
        labels: dict[str, str],
        payload: dict[str, Any],
    ) -> None:
        project = os.environ.get("PROJECT", "fleet-sre-demo")
        self._insert_seq += 1
        token = self._ids_insert.getrandbits(32)
        self.sink.write(
            LogEntry(
                insertId=f"{token:08x}-{self._insert_seq:08x}",
                timestamp=at,
                severity=severity,
                logName=f"projects/{project}/logs/chronicle-soar",
                resource=MonitoredResource(
                    type="k8s_container",
                    labels={
                        "project_id": project,
                        "location": "us-central1",
                        "cluster_name": "mock-soar",
                        "namespace_name": "chronicle-soar",
                        "pod_name": (
                            "python-0" if container == "python" else "etl-0"
                        ),
                        "container_name": container,
                    },
                ),
                labels=labels,
                jsonPayload=payload,
            )
        )

    def _connector_labels(self, st: _Connector) -> dict[str, str]:
        integ_spec = self.fleet.integrations.get(st.integration, {})
        version = str(integ_spec.get("version", "1.0")) if isinstance(
            integ_spec, dict
        ) else "1.0"
        return {
            "integration_name": st.integration,
            "integration_version": st.integration_version or version,
            "connector_name": st.display_name,
            "connector_instance": st.instance_id,
        }

    def _job_labels(self, st: _Job) -> dict[str, str]:
        return {
            "integration_name": st.integration,
            "integration_version": "1.0",
            "job_name": st.display_name,
        }

    def _connector_param(self, st: _Connector, display: str) -> str:
        for param in st.params:
            if param.get("displayName") == display:
                return str(param.get("value", ""))
        return ""

    def _run_connector(
        self, at: datetime, name: str, force: bool = False
    ) -> dict[str, Any]:
        st = self._connectors[name]
        env = st.env
        labels = self._connector_labels(st)
        if not force and (not st.enabled or self._active(env, "connector_stall")):
            return {"ran": False, "run_id": "", "alerts": []}
        run_id = self._next_run_id()
        self._write(at, "INFO", "python", labels,
                    {"event": "connector.run.started", "runId": run_id,
                     "environment": env})
        if not self.credential_ok(env):
            self._write(at, "WARNING", "python", labels,
                        {"event": "connector.run.warning", "runId": run_id,
                         "environment": env, "message": _CRED_MESSAGE})
            self._write(at, "INFO", "python", labels,
                        {"event": "connector.run.completed", "runId": run_id,
                         "environment": env, "alertsFetched": 0})
            self._connector_record(at, st, "DEBUG",
                                   f"Polling workspace for run {run_id}")
            self._connector_record(at, st, "INFO",
                                   f"Run {run_id} fetched 0 incidents")
            self._connector_record(at, st, "WARNING", _CRED_MESSAGE)
            return {"ran": True, "run_id": run_id, "alerts": []}
        self._upstream.generate_until(env, at, self._rate(env, "severity_drift"))
        incidents = self._upstream.fetch(env)
        unknown = self._connector_param(st, "Unknown Severity Priority")
        alerts = []
        for incident in incidents:
            alert = incident_to_alert(incident, unknown)
            alert["environment"] = env
            alerts.append(alert)
            self._write(at, "INFO", "etl",
                        {"correlation_id": self._correlation_id()},
                        {"event": "alert.ingested", "environment": env,
                         "alert": alert})
        flap = self._rate(env, "connector_flap")
        errored = flap > 0 and self._connector_rng(st.instance_id).random() < flap
        if errored:
            self._write(at, "ERROR", "python", labels,
                        {"event": "connector.run.error", "runId": run_id,
                         "environment": env, "message": _FLAP_MESSAGE})
        self._write(at, "INFO", "python", labels,
                    {"event": "connector.run.completed", "runId": run_id,
                     "environment": env, "alertsFetched": len(alerts)})
        st.fetched_total += len(alerts)
        self._connector_record(at, st, "DEBUG",
                               f"Polling workspace for run {run_id}")
        self._connector_record(at, st, "INFO",
                               f"Run {run_id} fetched {len(alerts)} incidents")
        if errored:
            self._connector_record(at, st, "ERROR", _FLAP_MESSAGE)
        return {"ran": True, "run_id": run_id, "alerts": alerts}

    def _connector_record(
        self, at: datetime, st: _Connector, level: str, message: str
    ) -> None:
        if _ms(at) >= st.logging_until_ms:
            return
        self._log_seq += 1
        self._connector_logs[st.name].append(
            {
                "name": f"{st.name}/logs/log-{self._log_seq:08d}",
                "time": str(_ms(at)),
                "message": message,
                "connectorInstanceId": st.instance_id,
                "logLevel": level,
                "seq": self._log_seq,
            }
        )

    def _run_job(self, at: datetime, name: str, force: bool = False) -> dict[str, Any]:
        st = self._jobs[name]
        env = st.env
        if not force and (not st.enabled or self._active(env, "job_stall")):
            return {"ran": False, "run_id": ""}
        run_id = self._next_run_id()
        labels = self._job_labels(st)
        try:
            lookback = int(self._job_param(env, "Lookback Hours"))
        except ValueError:
            lookback = 24
        if self._active(env, "job_error"):
            self._write(at, "ERROR", "python", labels,
                        {"event": "job.run.error", "runId": run_id,
                         "environment": env, "message": _FEED_MESSAGE})
            self._write(at, "ERROR", "python", labels,
                        {"event": "job.run.completed", "runId": run_id,
                         "environment": env, "status": "ERROR",
                         "indicatorsSynced": 0})
            self._job_record(at, st, "ERROR", _FEED_MESSAGE)
            st.last_run_time = at
            st.last_run_status = "ERROR"
        else:
            count = fetch_indicators(
                lookback, self._job_rng(st.instance_id)
            )
            self._write(at, "INFO", "python", labels,
                        {"event": "job.run.completed", "runId": run_id,
                         "environment": env, "status": "SUCCESS",
                         "indicatorsSynced": count})
            self._job_record(
                at, st, "SUCCESS",
                f"Run {run_id} synced {count} indicators",
            )
            st.last_run_time = at
            st.last_run_status = "SUCCESS"
        if not force:
            st.next_scheduled = at + timedelta(seconds=st.interval_seconds)
        return {"ran": True, "run_id": run_id}

    def _job_record(
        self, at: datetime, st: _Job, status: str, message: str
    ) -> None:
        self._log_seq += 1
        self._job_logs[st.name].append(
            {
                "name": f"{st.name}/logs/log-{self._log_seq:08d}",
                "startTime": _iso(at),
                "endTime": _iso(at),
                "message": message,
                "jobIdentifier": (
                    f"{self.prefix}/integrations/{st.integration}/jobs/{st.job}"
                ),
                "integration": f"{self.prefix}/integrations/{st.integration}",
                "jobInstanceId": st.instance_id,
                "status": status,
                "seq": self._log_seq,
            }
        )

    def _job_param(self, env: str, display: str) -> str:
        for st in self._jobs.values():
            if st.env == env:
                for param in st.params:
                    if param.get("displayName") == display:
                        return str(param.get("value", ""))
        return ""

    def _set_job_param(self, env: str, display: str, value: str) -> None:
        """F4 path: same parameter write as a patch, bumps updateTime."""
        for st in self._jobs.values():
            if st.env == env:
                for param in st.params:
                    if param.get("displayName") == display:
                        param["value"] = value
                st.update_time = self.clock.now()

    # -- read API -------------------------------------------------------

    def list_environments(self) -> list[dict[str, Any]]:
        return [self.environment_view(self._envs[k]) for k in sorted(self._envs)]

    def get_environment(self, env_id: str) -> dict[str, Any]:
        try:
            return self.environment_view(self._envs[env_id])
        except KeyError:
            raise ApiError(404, "NOT_FOUND", f"unknown environment {env_id!r}") from None

    def environment_view(self, e: _Environment) -> dict[str, Any]:
        # Fields: specs/chronicle/environments.yaml
        # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.environments
        return {
            "name": e.name,
            "displayName": e.display_name,
            "description": e.description,
            "contact": "",  # invented value
            "contactEmails": list(e.contact_emails),
            "contactPhone": "",  # invented value
            "base64Image": "",  # invented value
            "retentionDuration": "2592000s",  # invented value
            "dynamicParameters": [],  # invented value
            "system": False,  # invented value
            "aliasesJson": "{}",  # invented value
            "instanceUri": self.prefix,  # invented value
            "dataAccessScopesJson": "{}",  # invented value
            "weight": 0,  # invented value
            "id": e.id,
        }

    def create_environment(self, body: dict[str, Any]) -> dict[str, Any]:
        base = _slug(str(body.get("displayName", "env")))
        env_id = base
        n = 1
        while env_id in self._envs:
            n += 1
            env_id = f"{base}-{n}"
        e = _Environment(
            name=f"{self.prefix}/environments/{env_id}",
            id=env_id,
            display_name=str(body.get("displayName", "")),
            description=str(body.get("description", "")),
            contact_emails=list(body.get("contactEmails", [])),
        )
        self._envs[env_id] = e
        return self.environment_view(e)

    def list_integration_instances(
        self, integration: str
    ) -> list[dict[str, Any]]:
        if integration not in self.fleet.integrations:
            raise ApiError(404, "NOT_FOUND",
                           f"unknown integration {integration!r}")
        return [
            self.integration_view(self._integrations[k])
            for k in sorted(self._integrations)
            if self._integrations[k].integration == integration
        ]

    def get_integration_instance(self, name: str) -> dict[str, Any]:
        try:
            return self.integration_view(self._integrations[name])
        except KeyError:
            raise ApiError(404, "NOT_FOUND", f"unknown resource {name!r}") from None

    def integration_view(self, i: _IntegrationInstance) -> dict[str, Any]:
        # Fields: specs/chronicle/integrationInstances.yaml
        # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.integrationInstances
        params = []
        for param in i.params:
            value = param["value"]
            if param.get("type") == "PASSWORD":
                value = "********"
            params.append(
                {
                    "id": param.get("id", ""),
                    "displayName": param.get("displayName", ""),
                    "type": param.get("type", ""),
                    "description": param.get("description", ""),
                    "propertyName": param.get("propertyName", ""),
                    "mandatory": param.get("mandatory", False),
                    "value": value,
                }
            )
        return {
            "name": i.name,
            "environment": f"{self.prefix}/environments/{i.env}",
            "displayName": i.display_name,
            "parameters": params,
            "integrationIdentifier": i.integration,  # invented value
            "identifier": i.instance_id,  # invented value
            "description": "",  # invented value
            "configured": True,  # invented value
            "remote": False,  # invented value
            "agent": "",  # invented value
            "systemDefault": False,  # invented value
        }

    def execute_test(self, name: str) -> dict[str, Any]:
        # Request/response: mocks.md §4.1
        # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.integrationInstances/executeTest
        inst = self._integrations.get(name)
        if inst is None:
            raise ApiError(404, "NOT_FOUND", f"unknown resource {name!r}")
        if self.credential_ok(inst.env):
            return {"successful": True, "message": "Connection test succeeded"}
        return {
            "successful": False,
            "message": "Connection test failed: 401 Unauthorized",
        }

    def _require_connector(self, name: str) -> _Connector:
        try:
            return self._connectors[name]
        except KeyError:
            raise ApiError(404, "NOT_FOUND", f"unknown resource {name!r}") from None

    def _require_parent(self, parent: str, kind: str, section: str) -> None:
        path = parent.replace(self.prefix + "/integrations/", "")
        parts = path.split(f"/{kind}/")
        if len(parts) != 2:
            raise ApiError(404, "NOT_FOUND", f"unknown parent {parent!r}")
        integration, key = parts
        spec = self.fleet.integrations.get(integration)
        if not isinstance(spec, dict) or key not in spec.get(section, {}):
            raise ApiError(404, "NOT_FOUND", f"unknown parent {parent!r}")

    def list_connector_instances(self, parent: str) -> list[dict[str, Any]]:
        self._require_parent(parent, "connectors", "connectors")
        return [
            self.connector_view(self._connectors[k])
            for k in sorted(self._connectors)
            if self._connectors[k].name.startswith(parent + "/connectorInstances/")
        ]

    def get_connector(self, name: str) -> dict[str, Any]:
        return self.connector_view(self._require_connector(name))

    def connector_view(self, st: _Connector) -> dict[str, Any]:
        # Fields: specs/chronicle/connectorInstances.yaml
        # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.connectors.connectorInstances
        params = []
        for param in st.params:
            value = param.get("value", "")
            if param.get("type") == "PASSWORD":
                value = "********"
            params.append(
                {
                    "value": value,
                    "displayName": param.get("displayName", ""),
                    "description": param.get("description", ""),
                    "mandatory": param.get("mandatory", False),
                    "advanced": param.get("advanced", False),
                    "type": param.get("type", ""),
                }
            )
        return {
            "name": st.name,
            "agent": "",  # invented value
            "environment": f"{self.prefix}/environments/{st.env}",
            "displayName": st.display_name,
            "description": "",  # invented value
            "intervalSeconds": st.interval_seconds,
            "documentationLink": "",  # invented value
            "allowList": [],  # invented value
            "productFieldName": st.product_field,
            "eventFieldName": st.event_field,
            "timeoutSeconds": 30,  # invented value
            "connectorDefinitionName": (
                f"{self.prefix}/integrations/{st.integration}/connectors/"
                f"{st.connector}"
            ),
            "integrationVersion": st.integration_version,
            "version": st.version,
            "loggingEnabledUntilUnixMs": str(st.logging_until_ms),
            "statistics": {
                "pastDayAlertsCount": st.fetched_total,
                "avgAlertsPerDay": st.fetched_total,
            },
            "parameters": params,
            "connectorId": st.instance_id,  # invented value
            "identifier": st.instance_id,  # invented value
            "id": st.instance_id,  # invented value
            "updateAvailable": False,  # invented value
            "custom": st.custom,
            "enabled": st.enabled,
            "remote": False,  # invented value
            # invented rule: LIVE while enabled; stalls and swallowed
            # errors do not change it.
            "status": "LIVE" if st.enabled else "NOT_LIVE",
            "integration": f"{self.prefix}/integrations/{st.integration}",
        }

    def _check_mask(self, mask: list[str], allowed: frozenset[str]) -> set[str]:
        fields = {f.strip() for f in mask if f.strip()}
        if not fields:
            raise ApiError(400, "INVALID_ARGUMENT", "updateMask is required")
        unknown = sorted(fields - allowed)
        if unknown:
            raise ApiError(
                400, "INVALID_ARGUMENT",
                f"unknown updateMask field(s): {', '.join(unknown)}",
            )
        return fields

    def patch_connector(
        self, name: str, mask: list[str], body: dict[str, Any]
    ) -> dict[str, Any]:
        st = self._require_connector(name)
        fields = self._check_mask(mask, CONNECTOR_PATCHABLE)
        if "integrationVersion" not in body:
            raise ApiError(400, "INVALID_ARGUMENT",
                           "integrationVersion is required on patch")
        if "enabled" in fields:
            if "enabled" not in body:
                raise ApiError(400, "INVALID_ARGUMENT",
                               "enabled is in updateMask but missing from body")
            was = st.enabled
            st.enabled = bool(body["enabled"])
            if st.enabled and not was:
                self._clear_stalls(st.env, "connector_stall")
        if "intervalSeconds" in fields:
            if "intervalSeconds" not in body:
                raise ApiError(400, "INVALID_ARGUMENT",
                               "intervalSeconds is in updateMask but missing")
            st.interval_seconds = int(body["intervalSeconds"])
        if "parameters" in fields:
            if "parameters" not in body:
                raise ApiError(400, "INVALID_ARGUMENT",
                               "parameters is in updateMask but missing")
            self._apply_connector_params(st, body["parameters"])
        if "displayName" in fields and "displayName" in body:
            st.display_name = str(body["displayName"])
        if "description" in fields and "description" in body:
            pass
        st.version += 1
        return self.connector_view(st)

    def _apply_connector_params(
        self, st: _Connector, params: list[dict[str, Any]]
    ) -> None:
        old = {p.get("displayName"): p for p in st.params}
        merged = []
        for param in params:
            value = param.get("value", "")
            stored = old.get(param.get("displayName"), {})
            # invented: PASSWORD round-trip keeps the stored value. The
            # decision uses the stored parameter's type, so a patch that
            # omits "type" cannot overwrite a secret with the mask.
            if stored.get("type") == "PASSWORD" and value == "********":
                value = stored.get("value", "")
            merged.append(
                {
                    "value": value,
                    "displayName": param.get("displayName", ""),
                    "description": param.get("description", ""),
                    "mandatory": bool(param.get("mandatory", False)),
                    "advanced": bool(param.get("advanced", False)),
                    "type": param.get("type", "STRING"),
                }
            )
        st.params = merged

    def create_connector_instance(
        self, parent: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        path = parent.replace(self.prefix + "/integrations/", "")
        parts = path.split("/connectors/")
        if len(parts) != 2:
            raise ApiError(404, "NOT_FOUND", f"unknown parent {parent!r}")
        integration, conn_key = parts
        spec = self.fleet.integrations.get(integration)
        if not isinstance(spec, dict) or conn_key not in spec.get("connectors", {}):
            raise ApiError(404, "NOT_FOUND", f"unknown parent {parent!r}")
        conn_def = spec["connectors"][conn_key]
        env_id = str(body.get("environment", "")).rsplit("/", 1)[-1]
        if env_id not in self.fleet.environments:
            raise ApiError(400, "INVALID_ARGUMENT",
                           "environment is required to create an instance")
        base = _slug(str(body.get("displayName", "connector")))
        instance_id = base
        n = 1
        existing = {c.instance_id for c in self._connectors.values()}
        while instance_id in existing:
            n += 1
            instance_id = f"{base}-{n}"
        st = self._add_connector(
            integration, conn_key, conn_def, env_id, instance_id,
            int(body.get("intervalSeconds", 60)), spec,
        )
        if "displayName" in body:
            st.display_name = str(body["displayName"])
        if "enabled" in body:
            st.enabled = bool(body["enabled"])
        if "parameters" in body:
            self._apply_connector_params(st, body["parameters"])
        grid = st.interval_seconds
        delta = (self.clock.now() - self._start).total_seconds()
        first = self._start + timedelta(seconds=(int(delta // grid) + 1) * grid)
        self._push(first, "connector", st.name)
        return self.connector_view(st)

    def run_connector_on_demand(
        self, name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        # Request/response: mocks.md §4.1
        # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1beta/projects.locations.instances.integrations.connectors.connectorInstances/runOnDemand
        self._require_connector(name)
        if "connectorInstance" not in body:
            raise ApiError(400, "INVALID_ARGUMENT",
                           "connectorInstance is required")
        # invented: the body's values are not used.
        result = self._run_connector(self.clock.now(), name, force=True)
        cases = []
        for alert in result["alerts"]:
            cases.append(
                {
                    "displayName": alert["ticket_id"],
                    "productName": alert["device_product"],
                    "startTime": str(alert["start_time"]),
                    "endTime": str(alert["end_time"]),
                    "eventCount": 1,
                    "fullCaseInfoAsJson": json.dumps(alert, sort_keys=True),
                }
            )
        return {
            "debugOutput": (
                f"Run {result['run_id']}: fetched "
                f"{len(result['alerts'])} incidents"
            ),
            "success": True,
            "sampleCases": cases,
        }

    def set_logs_collection(
        self, name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        # Request/response: mocks.md §4.1
        # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.connectors.connectorInstances/setLogsCollection
        st = self._require_connector(name)
        if "enabled" not in body:
            raise ApiError(400, "INVALID_ARGUMENT", "enabled is required")
        now_ms = _ms(self.clock.now())
        if bool(body["enabled"]):
            self._connector_logs[st.name] = []
            st.logging_until_ms = now_ms + 3 * 3600 * 1000
        else:
            st.logging_until_ms = now_ms
        return {"logEnableExpirationTime": str(st.logging_until_ms)}

    def list_connector_logs(
        self, name: str, page_size: int | None, page_token: str | None
    ) -> dict[str, Any]:
        st = self._require_connector(name)
        records = sorted(
            self._connector_logs[st.name],
            key=lambda r: (int(r["time"]), r["seq"]),
            reverse=True,
        )
        return self._paginate(records, page_size, page_token, "logs",
                             self.connector_log_view)

    def get_connector_log(self, name: str, log_id: str) -> dict[str, Any]:
        st = self._require_connector(name)
        full = f"{st.name}/logs/{log_id}"
        for rec in self._connector_logs[st.name]:
            if rec["name"] == full:
                return self.connector_log_view(rec)
        raise ApiError(404, "NOT_FOUND", f"unknown log {full!r}")

    def connector_log_view(self, rec: dict[str, Any]) -> dict[str, Any]:
        # Fields: specs/chronicle/connectorInstanceLogs.yaml
        # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1beta/projects.locations.instances.integrations.connectors.connectorInstances.logs
        return {
            "name": rec["name"],
            "time": rec["time"],
            "message": rec["message"],
            "connectorInstanceId": rec["connectorInstanceId"],
            "logLevel": rec["logLevel"],
        }

    def _paginate(
        self,
        records: list[dict[str, Any]],
        page_size: int | None,
        page_token: str | None,
        key: str,
        view: Any,
    ) -> dict[str, Any]:
        try:
            offset = int(page_token) if page_token else 0
        except ValueError:
            raise ApiError(400, "INVALID_ARGUMENT",
                           "invalid pageToken") from None
        if offset < 0:
            raise ApiError(400, "INVALID_ARGUMENT", "invalid pageToken")
        size = page_size if page_size is not None else len(records)
        page = records[offset:offset + size]
        out: dict[str, Any] = {key: [view(r) for r in page]}
        if offset + size < len(records):
            out["nextPageToken"] = str(offset + size)
        return out

    def _require_job(self, name: str) -> _Job:
        try:
            return self._jobs[name]
        except KeyError:
            raise ApiError(404, "NOT_FOUND", f"unknown resource {name!r}") from None

    def list_job_instances(self, parent: str) -> list[dict[str, Any]]:
        self._require_parent(parent, "jobs", "jobs")
        return [
            self.job_view(self._jobs[k])
            for k in sorted(self._jobs)
            if self._jobs[k].name.startswith(parent + "/jobInstances/")
        ]

    def get_job(self, name: str) -> dict[str, Any]:
        return self.job_view(self._require_job(name))

    def job_view(self, st: _Job) -> dict[str, Any]:
        # Fields: specs/chronicle/jobInstances.yaml
        # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.jobs.jobInstances
        params = [
            {
                "id": p.get("id", ""),
                "mandatory": p.get("mandatory", False),
                "value": p.get("value", ""),
                "displayName": p.get("displayName", ""),
                "type": p.get("type", ""),
            }
            for p in st.params
        ]
        return {
            "name": st.name,
            "createTime": _iso(st.create_time),
            "updateTime": _iso(st.update_time),
            "displayName": st.display_name,
            "intervalSeconds": st.interval_seconds,
            "script": "sync_indicators.py",  # invented value
            "author": "mock-soar",  # invented value
            "lastRunTime": _iso(st.last_run_time) if st.last_run_time else "",
            "parameters": params,
            "job": f"{self.prefix}/integrations/{st.integration}/jobs/{st.job}",
            "integration": f"{self.prefix}/integrations/{st.integration}",
            "advancedConfig": "",  # invented value
            "nextScheduledRunTime": (
                _iso(st.next_scheduled) if st.next_scheduled else ""
            ),
            "custom": True,  # invented value
            "enabled": st.enabled,
            "description": "",  # invented value
            "lastRunStatus": st.last_run_status,
            "agent": "",  # invented value
            "advanced": False,  # invented value
            "uniqueIdentifier": st.instance_id,  # invented value
            "documentationLink": "",  # invented value
        }

    def patch_job(
        self, name: str, mask: list[str], body: dict[str, Any]
    ) -> dict[str, Any]:
        st = self._require_job(name)
        fields = self._check_mask(mask, JOB_PATCHABLE)
        if "enabled" in fields:
            if "enabled" not in body:
                raise ApiError(400, "INVALID_ARGUMENT",
                               "enabled is in updateMask but missing from body")
            was = st.enabled
            st.enabled = bool(body["enabled"])
            if st.enabled and not was:
                self._clear_stalls(st.env, "job_stall")
        if "intervalSeconds" in fields:
            if "intervalSeconds" not in body:
                raise ApiError(400, "INVALID_ARGUMENT",
                               "intervalSeconds is in updateMask but missing")
            interval = int(body["intervalSeconds"])
            if interval < 60:
                raise ApiError(400, "INVALID_ARGUMENT",
                               "intervalSeconds must be at least 60")
            st.interval_seconds = interval
        if "parameters" in fields:
            if "parameters" not in body:
                raise ApiError(400, "INVALID_ARGUMENT",
                               "parameters is in updateMask but missing")
            merged = []
            for i, param in enumerate(body["parameters"]):
                merged.append(
                    {
                        "id": param.get("id", f"param-{i}"),
                        "mandatory": bool(param.get("mandatory", False)),
                        "value": param.get("value", ""),
                        "displayName": param.get("displayName", ""),
                        "type": param.get("type", "STRING"),
                    }
                )
            st.params = merged
        if "displayName" in fields and "displayName" in body:
            st.display_name = str(body["displayName"])
        st.update_time = self.clock.now()
        return self.job_view(st)

    def run_job_on_demand(self, name: str) -> dict[str, Any]:
        # Request/response: mocks.md §4.1
        # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1beta/projects.locations.instances.integrations.jobs.jobInstances/runOnDemand
        self._require_job(name)
        # invented: the body's values are not used.
        self._run_job(self.clock.now(), name, force=True)
        return {"success": True}

    def list_job_logs(
        self, name: str, page_size: int | None, page_token: str | None
    ) -> dict[str, Any]:
        st = self._require_job(name)
        records = sorted(
            self._job_logs[st.name], key=lambda r: r["seq"], reverse=True
        )
        return self._paginate(records, page_size, page_token, "logs",
                             self.job_log_view)

    def get_job_log(self, name: str, log_id: str) -> dict[str, Any]:
        st = self._require_job(name)
        full = f"{st.name}/logs/{log_id}"
        for rec in self._job_logs[st.name]:
            if rec["name"] == full:
                return self.job_log_view(rec)
        raise ApiError(404, "NOT_FOUND", f"unknown log {full!r}")

    def job_log_view(self, rec: dict[str, Any]) -> dict[str, Any]:
        # Fields: specs/chronicle/jobInstanceLogs.yaml
        # https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.jobs.jobInstances.logs
        return {
            "name": rec["name"],
            "startTime": rec["startTime"],
            "endTime": rec["endTime"],
            "message": rec["message"],
            "jobIdentifier": rec["jobIdentifier"],
            "integration": rec["integration"],
            "jobInstanceId": rec["jobInstanceId"],
            "status": rec["status"],
        }

    # -- snapshot -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        connectors = {}
        for name in sorted(self._connectors):
            st = self._connectors[name]
            connectors[name] = {
                "enabled": st.enabled,
                "environment": st.env,
                "fetchedTotal": st.fetched_total,
                "integrationVersion": st.integration_version,
                "intervalSeconds": st.interval_seconds,
                "loggingEnabledUntilUnixMs": st.logging_until_ms,
                "parameters": [
                    {k: p[k] for k in sorted(p)} for p in st.params
                ],
                "version": st.version,
            }
        jobs = {}
        for name in sorted(self._jobs):
            job = self._jobs[name]
            jobs[name] = {
                "enabled": job.enabled,
                "environment": job.env,
                "intervalSeconds": job.interval_seconds,
                "lastRunStatus": job.last_run_status,
                "lastRunTime": _iso(job.last_run_time) if job.last_run_time else "",
                "nextScheduledRunTime": (
                    _iso(job.next_scheduled) if job.next_scheduled else ""
                ),
                "parameters": [{k: p[k] for k in sorted(p)} for p in job.params],
                "updateTime": _iso(job.update_time),
            }
        clog = {
            name: [self.connector_log_view(r) for r in sorted(
                recs, key=lambda r: r["seq"])]
            for name, recs in sorted(self._connector_logs.items())
        }
        jlog = {
            name: [self.job_log_view(r) for r in sorted(
                recs, key=lambda r: r["seq"])]
            for name, recs in sorted(self._job_logs.items())
        }
        return {
            "clock": _iso(self.clock.now()),
            "connectorInstances": connectors,
            "connectorLogs": clog,
            "environments": sorted(self._envs),
            "faults": self.list_faults(),
            "integrationInstances": sorted(self._integrations),
            "jobInstanceLogs": jlog,
            "jobInstances": jobs,
        }
