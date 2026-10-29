# Fleet SRE — Mock Inventory

As of 2026-10-28. Companion to [`spec.md`](spec.md) (source of truth) and [`deliverables.md`](deliverables.md) (D3 builds everything here).

The mock simulates only what the SRE, the evaluator and the executor touch. Everything below is either **faithful** (copied from Google's or Microsoft's published docs, with a URL) or **invented** (labelled, because the docs do not publish it). §9 lists which is which.

---

## 1. What is being simulated

```
Fake Sentinel (one workspace per tenant) ──poll──▶ Connector runtime ──AlertInfo──▶ Ingestion (ETL)
        ▲                                               │                               │
        │ plant canary                                   ▼                               ▼
   Canary planter                              python-container logs           etl-container logs
                                                + ConnectorLog records          (correlation_id)
Fake TI feed ──▶ Job runtime (intel-sync) ──▶ python-container logs + JobInstanceLog records

Mock Chronicle API (control plane): read, patch, run, test ◀── SRE read tools / Executor writes
Fault injector (/_chaos): changes world behaviour on a schedule or on request
```

All of it runs in one Python process (`fleetsre.mock`), driven by a `World` object with an injectable `Clock` and a seeded RNG, so a scenario replays identically.

## 2. Fleet

| Environment | Display name | Services | Alert volume (`expectedAlertsPerHour`) | Notes |
| --- | --- | --- | --- | --- |
| `alpha` | Alpha Corp | alert-ingestion, intel-sync | 120 | High volume |
| `bravo` | Bravo Health | alert-ingestion, intel-sync | 30 | Medium |
| `charlie` | Charlie Retail | alert-ingestion | 2 | Deliberately quiet; a naive "no alerts in 90 min" detector false-alarms here |
| `canary` | MSSP Canary | alert-ingestion | 0 organic | MSSP-owned canary tenant (production canary story) |

Connector `intervalSeconds`: 60 for all (demo speed). Job `intervalSeconds`: 300 (docs minimum is 60). Defined in `fixtures/fleet.yaml` (D1). Tenant names are fictional.

## 3. Components

### 3.1 Fake Sentinel (upstream, invented transport, faithful payload)

One simulated workspace per environment. The connector polls it.

- **Payload shape** follows Microsoft Sentinel *Incidents – List*: `value[]` of incidents with `name`, `etag`, `type: "Microsoft.SecurityInsights/incidents"`, and `properties` including `title`, `description`, `severity`, `status`, `incidentNumber`, `createdTimeUtc`, `firstActivityTimeUtc`, `lastActivityTimeUtc`, `lastModifiedTimeUtc`, `providerName`, `providerIncidentId`, `labels`, `additionalData.alertsCount`. [source](https://learn.microsoft.com/en-us/rest/api/securityinsights/incidents/list)
- **Severity values**: `High`, `Medium`, `Low`, `Informational`.
- **Path**: simplified to `/_upstream/{env}/incidents?since=<iso8601>` (invented). The real ARM path is not needed.
- **Auth**: fake token endpoint `/_upstream/{env}/token` taking a client secret; returns 401 when the credential fault is active (invented).
- **Volume**: Poisson arrivals at the environment's rate, seeded.

### 3.2 Connector runtime (emulates the SOAR `python` container)

One worker per connector instance. Every `intervalSeconds` (while `enabled` and not stalled):

1. Get a token from the fake token endpoint using the instance's `Client Secret` parameter.
2. Fetch incidents since the stored checkpoint (dedupe by `providerIncidentId`).
3. Map each incident to an `AlertInfo` (faithful field names, [source](https://docs.cloud.google.com/chronicle/docs/soar/respond/ide/creating-a-custom-connector)):
   - `name` ← `properties.title`
   - `ticket_id` / `display_id` ← `properties.providerIncidentId`
   - `rule_generator` ← first label or `"Sentinel Analytics"` (must not be empty)
   - `priority` ← severity map: `High→80`, `Medium→60`, `Low→40`, `Informational→-1` (Google's scale is Informative −1, Low 40, Medium 60, High 80, Critical 100)
   - unknown severity → the `Unknown Severity Priority` parameter if set, **otherwise the raw string is passed through** (this is the severity-drift bug)
   - `device_vendor = "Microsoft"`, `device_product = "Azure Sentinel"`
   - `start_time` / `end_time` ← first/last activity (unix ms)
   - `environment` ← the instance's environment
   - `events` ← one event dict per incident with `device_product`
4. Hand alerts to ingestion.
5. Emit logs (see §5) and append `ConnectorLog` records.

**Swallowed failure (Green Liar).** On a 401 from upstream, the worker catches the exception, writes a `WARNING` log and a `WARNING` ConnectorLog ("Failed to authenticate to Sentinel workspace: 401 Unauthorized"), returns zero alerts and still emits `connector.run.completed`. Status stays `LIVE`.

**Custom connector definition (invented — not the Marketplace integration's real parameters).**

| Integration | `MockSentinel` (custom: true), version `1.0` |
| --- | --- |
| Connector | `Mock Sentinel Incidents Connector` |
| `productFieldName` / `eventFieldName` | `device_product` / `event_name` |

Connector instance parameters (`ConnectorInstanceParameter`: `value`, `displayName`, `description`, `mandatory`, `advanced`, `type`):

| displayName | type | mandatory | default |
| --- | --- | --- | --- |
| Tenant ID | STRING | yes | per env |
| Client ID | STRING | yes | per env |
| Client Secret | PASSWORD | yes | per env |
| Workspace Name | STRING | yes | per env |
| Max Incidents Per Cycle | INT | no | `50` |
| Unknown Severity Priority | INT | no | `""` (empty) |

### 3.3 Ingestion (emulates the SOAR `etl` container)

Receives `AlertInfo` objects. For each: assign a `correlation_id` (UUID4 from the seeded RNG), emit one `etl` log entry `alert.ingested` with the alert in `jsonPayload.alert` (fields: `name`, `ticket_id`, `rule_generator`, `priority`, `device_vendor`, `device_product`, `environment`, `start_time`, `end_time`, `canary`). Filtering etl logs by `correlation_id` traces one alert's ingestion, as Google documents. [source](https://docs.cloud.google.com/chronicle/docs/secops/collect-secops-soar-logs)

Ingestion does **not** validate `priority`; a string passes straight through (realistic, and the point of the schema tripwire).

### 3.4 Job runtime (intel-sync)

One worker per job instance. Every `intervalSeconds`: fetch indicators from a fake TI feed (`/_upstream/{env}/indicators?lookbackHours=N`), "sync" them, write a `python` log entry `job.run.completed` with `jsonPayload.indicatorsSynced` and `jsonPayload.status`, and append a `JobInstanceLog` (`status` `SUCCESS`/`ERROR`, `message`, `startTime`, `endTime`). Updates the job instance's `lastRunTime`, `lastRunStatus`, `nextScheduledRunTime`.

Job definition (invented): integration `MockThreatIntel` v`1.0`, job `Sync Indicators`. Parameters:

| displayName | type | mandatory | default |
| --- | --- | --- | --- |
| Feed URL | URL | yes | `/_upstream/{env}/indicators` |
| Lookback Hours | INT | yes | `24` |

`Lookback Hours = 0` returns zero indicators with status `SUCCESS` (the hollow-success bug).

### 3.5 Canary planter

`POST /_upstream/{env}/incidents:plant` creates an incident whose title starts with `[CANARY]` and whose `providerIncidentId` is `canary-<uuid>`. Ingestion sets `jsonPayload.alert.canary = true` for it. The platform's synthetic tripwire calls this endpoint; in production it would target only the `canary` environment (spec §5).

## 4. Mock Chronicle API (control plane)

FastAPI app `fleetsre.mock.chronicle_api`. Google-style REST: base `/v1/`, resource names in the path, custom methods as `POST /v1/{name}:method`, `PATCH` with an `updateMask` query parameter, list with `pageSize` / `pageToken` returning `nextPageToken`. Errors use Google's shape: `{"error": {"code": 404, "message": "...", "status": "NOT_FOUND"}}`.

Fixed prefix: `projects/fleet-sre-demo/locations/us/instances/mock-soar`.

### 4.1 Resources, methods and fields

Only these. Field lists are copied from the v1/v1beta reference; implement every listed field (populate with realistic values), add nothing.

**environments** — `projects/{p}/locations/{l}/instances/{i}/environments/{environment}` — [docs](https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.environments)
- Methods: `get`, `list`, `create`
- Fields: `name`, `displayName`, `description`, `contact`, `contactEmails`, `contactPhone`, `base64Image`, `retentionDuration`, `dynamicParameters[]`, `system`, `aliasesJson`, `instanceUri`, `dataAccessScopesJson`, `weight`, `id`

**integrationInstances** — `.../integrations/{integration}/integrationInstances/{instance}` — [docs](https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.integrationInstances)
- Methods: `get`, `list`, `executeTest`
- Fields: `name`, `environment`, `displayName`, `parameters[]` (`id`, `displayName`, `type`, `description`, `propertyName`, `mandatory`, `value`), `integrationIdentifier`, `identifier`, `description`, `configured`, `remote`, `agent`, `systemDefault`
- `executeTest` (invented response body): `{"success": bool, "message": string}`. Calls the fake token endpoint with the environment's credential. Changes no state.

**connectorInstances** — `.../integrations/{integration}/connectors/{connector}/connectorInstances/{connectorInstance}` — [docs](https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.connectors.connectorInstances)
- Methods: `get`, `list`, `create`, `patch`, `runOnDemand`, `setLogsCollection`
- Fields: `name`, `agent`, `environment`, `displayName`, `description`, `intervalSeconds`, `documentationLink`, `allowList[]`, `productFieldName`, `eventFieldName`, `timeoutSeconds`, `connectorDefinitionName`, `integrationVersion`, `version`, `loggingEnabledUntilUnixMs`, `statistics` (`pastDayAlertsCount`, `avgAlertsPerDay`), `parameters[]` (`value`, `displayName`, `description`, `mandatory`, `advanced`, `type`), `connectorId`, `identifier`, `id`, `updateAvailable`, `custom`, `enabled`, `remote`, `status` (`LIVE` | `NOT_LIVE`), `integration`
- `patch`: `integrationVersion` is required on patch (per docs); 400 without it. Every successful patch increments `version`.
- `PASSWORD` parameter values are returned as `"********"` (mock policy).
- `status` semantics are not documented. Mock rule: `LIVE` while `enabled`, `NOT_LIVE` when disabled. Stalls and swallowed errors do not change it.
- `statistics` are daily counts, too coarse to see a stall within minutes.

**connectorInstances.logs** — `.../connectorInstances/{connectorInstance}/logs/{log}` — [docs](https://docs.cloud.google.com/chronicle/docs/reference/rest/v1beta/projects.locations.instances.integrations.connectors.connectorInstances.logs)
- Methods: `get`, `list` (newest first)
- Fields: `name`, `time` (int64 string, unix ms), `message`, `connectorInstanceId`, `logLevel` (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `ALERT`, `VERBOSE`)
- `DEBUG` records are only written while `loggingEnabledUntilUnixMs` is in the future (set by `setLogsCollection`).

**jobInstances** — `.../integrations/{integration}/jobs/{job}/jobInstances/{jobInstance}` — [docs](https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.jobs.jobInstances)
- Methods: `get`, `list`, `patch`, `runOnDemand`
- Fields: `name`, `createTime`, `updateTime`, `displayName`, `intervalSeconds` (min 60), `script`, `author`, `lastRunTime`, `parameters[]` (`id`, `mandatory`, `value`, `displayName`, `type`), `job`, `integration`, `advancedConfig`, `nextScheduledRunTime`, `custom`, `enabled`, `description`, `lastRunStatus` (`SUCCESS` | `ERROR`), `agent`, `advanced`, `uniqueIdentifier`, `documentationLink`
- `patch` with `intervalSeconds < 60` → 400.

**jobInstances.logs** — `.../jobInstances/{jobInstance}/logs/{log}` — [docs](https://docs.cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.integrations.jobs.jobInstances.logs)
- Methods: `get`, `list` (newest first)
- Fields: `name`, `startTime`, `endTime`, `message`, `jobIdentifier`, `integration`, `jobInstanceId`, `status` (`SUCCESS` | `ERROR`)
- Note: the docs' name format says `integrationJobInstanceLogs/{id}` while the resource path is `logs`; the mock serves `logs`.

**Not mocked:** cases, alerts, playbooks, environment groups, connector definitions, integrations (beyond the fixed path segment), anything SIEM.

### 4.2 Authorization

Every request carries an identity. Local dev: header `X-Fleetsre-Identity: obs-reader|deployer` (only when `FLEETSRE_DEV_AUTH=1`). Deployed: Google ID token; the service-account email maps to an identity. Permission names follow the documented pattern `chronicle.<resource>.<verb>` (e.g. `chronicle.connectorInstanceLogs.get` is the documented permission for `logs.get`).

| Identity | Permissions |
| --- | --- |
| `obs-reader` | `chronicle.environments.{get,list}`, `chronicle.integrationInstances.{get,list,executeTest}`, `chronicle.connectorInstances.{get,list}`, `chronicle.connectorInstanceLogs.{get,list}`, `chronicle.jobInstances.{get,list}`, `chronicle.jobInstanceLogs.{get,list}` |
| `deployer` | all of `obs-reader` plus `chronicle.connectorInstances.{create,update,runOnDemand,setLogsCollection}`, `chronicle.jobInstances.{update,runOnDemand}`, `chronicle.environments.create` |

Missing permission → 403 `PERMISSION_DENIED`. Exact permission strings beyond the documented one are assumptions; check them on D1 day where docs pages show them.

## 5. Log format

Every log line, local or in Cloud Logging, is a Cloud Logging `LogEntry`:

```json
{
  "insertId": "a1b2c3d4e5",
  "timestamp": "2026-10-29T14:00:05.123Z",
  "severity": "WARNING",
  "logName": "projects/fleet-sre-demo/logs/chronicle-soar",
  "resource": {
    "type": "k8s_container",
    "labels": {
      "project_id": "fleet-sre-demo",
      "location": "us-central1",
      "cluster_name": "mock-soar",
      "namespace_name": "chronicle-soar",
      "pod_name": "python-0",
      "container_name": "python"
    }
  },
  "labels": {
    "integration_name": "MockSentinel",
    "integration_version": "1.0",
    "connector_name": "Mock Sentinel Incidents Connector",
    "connector_instance": "alpha-sentinel"
  },
  "jsonPayload": {
    "event": "connector.run.completed",
    "runId": "run-000123",
    "environment": "alpha",
    "alertsFetched": 0,
    "message": "Failed to authenticate to Sentinel workspace: 401 Unauthorized"
  }
}
```

Faithful: `namespace_name = "chronicle-soar"`, `container_name` ∈ {`playbook`, `python`, `etl`}, and the entry `labels`: connector logs carry `integration_name`, `integration_version`, `connector_name`, `connector_instance`; job logs carry `integration_name`, `integration_version`, `job_name`; etl logs carry `correlation_id`. [source](https://docs.cloud.google.com/chronicle/docs/secops/collect-secops-soar-logs)

Invented: `logName`, `pod_name`, `cluster_name`, every `jsonPayload` field, and the `event` names below.

| `event` | Container | Labels | Key payload fields |
| --- | --- | --- | --- |
| `connector.run.started` | python | connector | `runId`, `environment` |
| `connector.run.completed` | python | connector | `runId`, `alertsFetched`, `environment` |
| `connector.run.warning` | python | connector | `runId`, `message` (severity WARNING) |
| `connector.run.error` | python | connector | `runId`, `message` (severity ERROR) |
| `alert.ingested` | etl | `correlation_id` | `alert{...}`, `environment` |
| `job.run.completed` | python | job | `runId`, `status`, `indicatorsSynced`, `environment` |
| `job.run.error` | python | job | `runId`, `message` (severity ERROR) |

Environment is not a documented log label. The platform maps `connector_instance` / `job_name` + environment through the API; the mock also puts `environment` in `jsonPayload` for convenience (invented).

Local sink: append-only JSONL, one `LogEntry` per line, `insertId` unique per entry (seeded).

## 6. Fault catalog

Each fault has a defined cause, what the native API shows, what should trip, and the **right answer**: the action a correct SRE takes. The right answers make the evaluations gradeable.

| ID | Fault | Mock behaviour | Native API shows | Should trip | Right answer |
| --- | --- | --- | --- | --- | --- |
| F1 | `connector_stall` | Worker for one instance stops running. No logs, no ConnectorLogs | `status: LIVE`, `enabled: true` | `dead-connector` (cadence) | `connectorInstances.restart`. The mock clears a stall on any `enabled` false→true transition |
| F2 | `credential_expiry` | Upstream token endpoint returns 401 for one env. Connector swallows it: WARNING logs and ConnectorLogs every run, zero alerts, runs on schedule | `status: LIVE`; `executeTest` → `success: false` | `connector-errors` (errorSignature), `canary` (synthetic) | `escalate`, category `credential_failure`. Siblings healthy → tenant-specific. Fix (rotate secret) is outside the allowlist |
| F3 | `severity_drift` | Upstream starts emitting an unmapped severity (`"Critical"`); with `Unknown Severity Priority` empty, `priority` becomes a string | `status: LIVE`, volume normal | `priority-shape` (schema) | `connectorInstances.patch` `parameters`: set `Unknown Severity Priority` to `100` (or `80`) |
| F4 | `job_hollow_success` | Job parameter `Lookback Hours` set to `0` (simulates a bad config change, visible in `tripwire_history` as a recent change); runs succeed with 0 indicators | `lastRunStatus: SUCCESS` | intel-sync `hollow-run` (schema on `indicatorsSynced > 0` over N runs) | `jobInstances.patch` `parameters`: `Lookback Hours` → `24` |
| F5 | `job_error` | Fake TI feed returns 500; job runs end `ERROR` | `lastRunStatus: ERROR` | intel-sync `job-errors` (errorSignature) | `escalate`, category `job_failing` (upstream outage) |
| F6 | `connector_flap` | Instance emits ERROR on a fraction of runs (`rate` in the fault body, e.g. 0.2 or 0.6) | `status: LIVE` | `connector-errors` only if threshold met | Harness-only scenarios for false-positive and threshold tuning |
| F7 | `job_stall` | Job worker stops running; `nextScheduledRunTime` goes stale | `lastRunStatus` unchanged, `enabled: true` | intel-sync `dead-job` (cadence) | `jobInstances.restart`, category `job_stalled`. The mock clears a job stall on any `enabled` false→true transition |

Clearing: `DELETE /_chaos/faults/{id}` restores normal behaviour within 2 intervals. F2 has no allowlisted fix; in the demo, the admin UI "mark rotated" button clears it.

## 7. Chaos API

Enabled only when `FLEETSRE_CHAOS=1`; otherwise every `/_chaos` route returns 404.

| Method | Path | Body | Effect |
| --- | --- | --- | --- |
| `POST` | `/_chaos/faults` | `{"type": "credential_expiry", "environment": "bravo", "target": "<resource name, optional>"}` | Activates a fault; returns `{"id": "..."}` |
| `GET` | `/_chaos/faults` | | Active faults |
| `DELETE` | `/_chaos/faults/{id}` | | Clears a fault |
| `POST` | `/_chaos/schedule` | list of `{at, type, environment, clearAt?}` | Scenario schedule (harness and the live auto-chaos loop) |

## 8. Determinism and time

- `World(seed, clock, sink, fleet)`; `world.step_until(t)` advances a `SimClock` and runs every due worker in timestamp order.
- Same seed + same fleet + same fault schedule → byte-identical JSONL.
- Wall-clock mode (`world.run_forever()`) is used for the live demo only.

## 9. Faithful versus invented

| Faithful (cite in code comments) | Invented (say so in the README) |
| --- | --- |
| Resource names, paths, methods and field lists (§4.1) | Fake Sentinel transport and token endpoint |
| `status` enum values, `logLevel` and `JobResultStatus` enums, `ParamType` enum | Meaning of `LIVE` (mock rule in §4.1) |
| `integrationVersion` required on connector patch; job `intervalSeconds` minimum 60 | `executeTest` response body |
| Permission name pattern (`chronicle.connectorInstanceLogs.get`) | Other permission strings, until verified |
| Log namespace, containers and entry labels (§5) | `logName`, `pod_name`, every `jsonPayload` field, event names |
| AlertInfo field names and the priority scale | The custom connector's and job's parameters |
| Sentinel incident payload fields and severities | Fault mechanics (§6) |
