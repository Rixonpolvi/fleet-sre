# Fleet SRE — Deliverables and Acceptance Tests

As of 2026-10-28. Companion to [`spec.md`](spec.md) (source of truth) and [`mocks.md`](mocks.md).

Revised 2026-10-28 (afternoon) to match the spec revision: `.agents/AGENTS.md`, held-out scenarios (D1), the spike in the gated loop shape (D2), the gated investigator (D5), NEEDS_INPUT, reject with notes and the executor journal (D6), and the turn timeline (D8).

Revised 2026-10-29 during D1: contract rules, scenario values, `ContractError` codes and evaluator rules settled by the D1 fixtures (§D1, D3-T21, D3-T22, D4-T7, D4-T11, D4-T15).

Nine deliverables, D0–D8. Each one is **two merges**: a tests PR, then an implementation PR. The acceptance tests below are the definition of done. The coding agent translates them into code; it does not decide them.

---

## 1. How to run a deliverable

### 1.1 Two sessions, two PRs

| Step | Who | Branch | What happens | Merge gate |
| --- | --- | --- | --- | --- |
| A. Tests | Muse Code, fresh session | `tests/dN` | Translates this file's test table for DN into `tests/acceptance/dN/`. Adds interface stubs (signatures from the DN section that raise `NotImplementedError`) so tests collect. Writes no implementation. | You review (§1.3). Every new test fails for the right reason: `NotImplementedError` or a missing fixture, never a syntax or import error in the test itself. |
| B. Implementation | Muse Code, fresh session | `impl/dN` | Implements until DN's acceptance tests pass. May add `tests/unit/`. May not touch protected paths. | CI green: `make check`, all acceptance suites D0..DN (regression), protected-path guard, trace check. You review the diff. |

Why two sessions: the session that writes the code must not be able to weaken the tests. The protected-path guard (D0) enforces it mechanically; the fresh session keeps context small and the diff reviewable.

Commit messages start with the ID (`D3-tests: …`, `D3-impl: …`). That history is your hackathon provenance. After each session, export it with `muse export` to `provenance/DN-tests.json` or `provenance/DN-impl.json`, and commit it once `scan_secrets.py` and the denylist hook pass.

### 1.2 Conventions

- **Test names:** `test_dN_tM_<slug>`, one function per row, docstring starts with the ID (`"""D3-T17: …"""`). Parametrised rows stay one function.
- **Markers:** default tests run offline in `make check`. `@pytest.mark.live` calls the Meta Model API (costs money; run with `make test-live`). `@pytest.mark.gcp` needs the GCP project (run with `make test-gcp`). Live and GCP results are committed under `evals/` with a date.
- **Clock:** all behavioural tests use `SimClock`. No `sleep` in tests.
- **Seeds:** tests that use the mock world pass an explicit seed.
- **Protected paths** (read-only in `impl/*` branches): `contracts/`, `fixtures/`, `specs/`, `prompts/`, `tests/acceptance/`, `docs/`, `.agents/`.

### 1.3 Your review checklist (tests PR)

1. Every ID in the DN table has exactly one test function; nothing extra in `tests/acceptance/dN/`.
2. Each test asserts the stated pass condition, not a weaker one (look for `assert True`, broad `except`, assertions on mocks of the thing under test, tolerances wider than stated).
3. Tests use only the public interfaces in the DN section.
4. Run it: all DN tests fail with `NotImplementedError` or a missing-fixture error.

### 1.4 Session briefs (paste into `/goal`)

**Session A (tests)**
```
Objective: Write the acceptance tests for DN exactly as specified in docs/deliverables.md §DN.
Inputs: docs/spec.md, docs/mocks.md, docs/deliverables.md §1 and §DN, .agents/AGENTS.md.
Deliverables: tests/acceptance/dN/*.py (one function per test ID), interface stubs for the
  signatures in §DN that raise NotImplementedError, any fixtures §DN marks "Session A".
Rules: no implementation logic. Do not edit docs/, contracts/, fixtures/ (except files §DN marks
  "Session A"), or other deliverables' tests.
Acceptance: pytest tests/acceptance/dN collects every test; every test fails with
  NotImplementedError or a missing-fixture error; make lint passes.
Stop: print a table of test ID -> function name -> current failure reason, then stop.
```

**Session B (implementation)**
```
Objective: Implement DN so that tests/acceptance/dN passes, per docs/deliverables.md §DN.
Inputs: docs/spec.md, docs/mocks.md, docs/deliverables.md §DN, .agents/AGENTS.md, tests/acceptance/.
Rules: protected paths are read-only (.agents/AGENTS.md). Do not modify any acceptance test. You may
  add tests under tests/unit/. Out of scope: everything in later deliverables.
Acceptance: make check passes; pytest tests/acceptance/d0 ... dN all pass.
Stop: print the acceptance results and a summary of files changed, then stop. Do not start DN+1.
```

## 2. Repository layout

```
fleet-sre/
  .agents/AGENTS.md  README.md  Makefile  pyproject.toml  .importlinter
  docs/                spec.md mocks.md deliverables.md decisions/
  prompts/             investigate.md author.md (human-written SRE prompts)
  contracts/           schema.json alert-ingestion.json intel-sync.json
  specs/chronicle/     *.yaml (field lists with source URLs)
  fixtures/            fleet.yaml logs/samples/ golden/ scenarios/ invalid/ mutants/ spike/ scripted/ recorded/
  fleetsre/
    logmodel.py clock.py ports.py contracts.py registry.py
    mock/        world.py fleet.py sentinel.py connector.py ingestion.py jobs.py chronicle_api.py chaos.py auth.py
    evaluator/   tripwires.py engine.py canary.py
    harness/     scenario.py replay.py scorecard.py
    sre/         schemas.py session.py gates.py tools.py gateway.py investigator.py
    control/     incidents.py approvals.py executor.py journal.py verify.py
    authoring/   loop.py amend.py
    adapters/    jsonl.py sqlite_state.py cloud_logging.py pubsub_push.py firestore_state.py chronicle_http.py
    web/         app.py templates/
    live/        chaos_loop.py
  tests/acceptance/d0 … d8/   tests/unit/
  evals/  spikes/  infra/  scripts/  provenance/
```

Stack: Python 3.12, FastAPI, pydantic v2, httpx, pytest, ruff, mypy, import-linter, jsonschema, PyYAML. The model gateway uses the OpenAI Python SDK's Responses API against the Meta Model API base URL (`spec.md` §8).

## 3. Schedule

| Day | Deliverables |
| --- | --- |
| Wed Oct 28 | D0, D1, D2 |
| Thu Oct 29 | D3, D4 |
| Fri Oct 30 | D5 |
| Sat Oct 31 | D6 |
| Sun Nov 1 | D7 |
| Mon Nov 2 | D8 |
| Tue Nov 3 | Freeze: README, video, final evals |
| Wed Nov 4 | Submit by 9:00 PM ET |

---

## D0 — Scaffold and guardrails

**Target:** Wed Oct 28 · **Owner:** Muse (both sessions) · **Depends on:** nothing

**Goal.** A repo where `make check` works, protected paths are enforced in CI, and every acceptance test is traceable to this file.

**Interfaces / files.** `Makefile` targets `check`, `lint`, `test`, `test-live`, `test-gcp`, `demo` (placeholder until D8). `scripts/check_protected.py --base <ref> --head <ref>`, `scripts/check_trace.py --doc docs/deliverables.md --tests tests/acceptance`, `scripts/scan_secrets.py <paths>`, `.pre-commit-config.yaml` with the denylist hook, `.github/workflows/ci.yml`, `.agents/AGENTS.md` (draft at the end of this section). Muse Code loads standing rules from `.agents/AGENTS.md` at session start and treats `.agents/` as a guardrail path, so a write to it always stops for your review.

| ID | Test | Pass condition |
| --- | --- | --- |
| D0-T1 | `make check` on a fresh clone | Exits 0; runs ruff, mypy, import-linter, trace check and offline pytest |
| D0-T2 | Package imports | `import fleetsre` works; `fleetsre.__version__` is set; `pyproject.toml` requires Python ≥ 3.12 |
| D0-T3 | Protected-path guard detects edits | In a temp git repo, `check_protected.py` exits 1 when the head changes any file under `contracts/`, `fixtures/`, `specs/`, `prompts/`, `tests/acceptance/`, `docs/` or `.agents/`; exits 0 for changes elsewhere |
| D0-T4 | Guard wired into CI | `ci.yml` parses; it has a job that runs `check_protected.py` on pull requests from branches matching `impl/*` |
| D0-T5 | Trace check | Against a temp doc and temp tests dir, `check_trace.py` exits 1 when an ID in a section that has a `tests/acceptance/dN/` directory lacks a `test_dN_tM_*` function, and 0 when all exist |
| D0-T6 | Secret scan | `scan_secrets.py` flags a temp file containing a PEM private key, a GCP service-account key JSON, and an API-key-shaped string; exits 0 on the repo |
| D0-T7 | Denylist hook | The pre-commit hook reads terms from `~/.config/fleetsre/denylist.txt` (outside the repo) and blocks a staged file containing a term; the repo contains no denylist file |
| D0-T8 | Markers | pytest registers `live` and `gcp`; `make check` deselects them; `make test-live` and `make test-gcp` select only them |
| D0-T9 | AGENTS.md structure | `.agents/AGENTS.md` exists, there is no `AGENTS.md` at the repo root, and it contains the headings Project, Hard rules, Protected paths, Architecture, Commands, Deliverable protocol |

**Out of scope:** any `fleetsre` logic.

**`.agents/AGENTS.md` draft** (Session B may adjust wording, not rules):

```markdown
# Project
Fleet SRE: an agentic SRE for multi-tenant Google SecOps SOAR, built on a local-first platform with a
mock SecOps that follows Google's published Chronicle API and SOAR log format. Spec: docs/spec.md.

# Hard rules
- Synthetic data only. No real company, client or employer names, systems, code or data.
- No secrets in the repo. Credentials come from the environment or impersonation.
- Every mock resource field must exist in specs/chronicle/*.yaml; cite the doc URL in a comment
  on each mock resource model.
- Anything not in Google's docs is invented: mark it with a `# invented:` comment.
- fleetsre.sre must not import google.*, fleetsre.adapters or fleetsre.mock. It reaches the world
  only through fleetsre.sre.tools.ToolSet.
- Checks run inside the tools (docs/spec.md §6.3, §6.5). Never add a code path that lets the model
  skip a gate, and never hard-code which tool the model calls when.
- Use SimClock in tests. No sleeps.

# Protected paths (read-only on impl/* branches)
contracts/ fixtures/ specs/ prompts/ tests/acceptance/ docs/ .agents/

# Architecture
Ports and adapters (docs/spec.md §4.2). Local adapters first; GCP adapters in D7 only.

# Commands
make check | make test-live | make test-gcp | make demo

# Deliverable protocol
Work only on the deliverable named in the goal. Tests sessions write tests and stubs only.
Implementation sessions never modify acceptance tests. Stop when the goal's acceptance passes and
print the summary the goal asks for. Never start the next deliverable.
```

---

## D1 — Contracts, log contract, fleet and scenarios

**Target:** Wed Oct 28 · **Owner:** **You** write the contracts, scenarios, golden fixtures, mutants and the decision record. Muse writes the tests (Session A) and the loader/validator code (Session B). You may let Muse draft `schema.json` and `specs/chronicle/*.yaml`; you approve every line. · **Depends on:** D0

**Goal.** Everything that defines "correct" exists, is reviewed, and validates.

**Interfaces.**
```python
# fleetsre/logmodel.py
class LogEntry(BaseModel): insertId: str; timestamp: datetime; severity: str; logName: str
    resource: MonitoredResource; labels: dict[str, str]; jsonPayload: dict[str, Any]
def read_jsonl(path) -> list[LogEntry]

# fleetsre/contracts.py
class Contract(BaseModel): ...          # mirrors contracts/schema.json
class ContractError(Exception): code: str
def load_contract(path) -> Contract     # schema + referential checks; raises ContractError(code)

# fleetsre/harness/scenario.py
class Scenario(BaseModel): id; service; seed; durationSeconds; deployments; faults; expected; holdout: bool = False
def load_scenario(path) -> Scenario

# fleetsre/mock/fleet.py
def load_fleet(path) -> FleetConfig
```

**Files you author** (protected once merged):
- `contracts/alert-ingestion.json` (tripwires: `dead-connector`, `canary`, `priority-shape`, `connector-errors`) and `contracts/intel-sync.json` (tripwires: `dead-job`, `hollow-run`, `job-errors`). Schema tripwires support `event`, `where`, `consecutive: K` (trip after K consecutive violating entries; default 1) and `recoverAfter` (valid entries in a row to recover; default `consecutive`); see `spec.md` §5.
- `fixtures/fleet.yaml` per `mocks.md` §2.
- `fixtures/logs/samples/*.jsonl`: a few entries per event type in `mocks.md` §5.
- `fixtures/scenarios/<service>/*.yaml` (format below).
- `fixtures/golden/*.jsonl` + `*.expected.json`: four tiny streams (≤ 40 lines each) with hand-checked expected events: `cadence-stall`, `schema-break`, `error-burst`, `canary-miss`.
- `fixtures/invalid/*.json`: broken contracts, each with `"_expectError": "<code>"`.
- `fixtures/mutants/*.json`: weakened copies of the real contracts, each with `"_mutation": "<what was weakened>"` (e.g. `maxSilentIntervals: 50`, schema tripwire removed, canary deadline ×10, errorSignature `minSeverity` raised to ERROR).
- `docs/decisions/logging-resource.md`: result of the manual check below.

**Manual check (15 minutes, today).** From your laptop, write one entry with a 10-line Python script using `google-cloud-logging`, passing `resource=Resource(type="k8s_container", labels={"project_id": ..., "location": ..., "cluster_name": "mock-soar", "namespace_name": "chronicle-soar", "pod_name": "python-0", "container_name": "python"})` and the connector entry labels. Then query it in Logs Explorer with `resource.labels.namespace_name="chronicle-soar"`. Record whether it landed with those resource labels. If it was rejected or rewritten, record the fallback (`generic_task` with the same entry `labels`).

**Scenario format.**
```yaml
id: green-liar
service: alert-ingestion
seed: 7
durationSeconds: 7200
holdout: false
deployments: [alpha, bravo, charlie]
faults:
  - { at: 1830, type: credential_expiry, environment: bravo }
expected:
  trips:
    - { deployment: bravo, tripwire: connector-errors, withinSeconds: 600 }
    - { deployment: bravo, tripwire: canary, withinSeconds: 900 }
  recoveries: []
  mustNotTrip:
    - { deployment: alpha, tripwire: "*" }
    - { deployment: charlie, tripwire: "*" }
```

**Required scenarios.**
- `alert-ingestion`: `clean` (all three, 6 h), `stall` (F1 alpha), `green-liar` (F2 bravo), `severity-drift` (F3 alpha), `flap-mild` (F6 alpha, rate 0.2, must not trip; **holdout**), `flap-severe` (F6 alpha, rate 0.7, trips `connector-errors`), `recovery` (F1 alpha then cleared; expects trip then recovery; **holdout**), `quiet-tenant` (charlie only, 6 h, no trips).
- `intel-sync`: `clean`, `job-stall` (F7), `hollow-success` (F4), `job-error` (F5), `recovery` (F7 then cleared; **holdout**).

**`ContractError` codes.** `load_contract` checks the schema first, then the referential rules in this order, and raises the first failure. Top-level keys starting with `_` (`_expectError`, `_mutation`) are allowed by the schema and ignored by the loader.

| Code | Raised when |
| --- | --- |
| `schema_invalid` | The contract fails `contracts/schema.json` |
| `unknown_component` | A tripwire's `component` is not a component ID |
| `duplicate_tripwire` | Two tripwires share an ID |
| `dangling_parameter` | A `${param}` in a spec is not in `parameters` |
| `unknown_override` | A `changePoint.overrides` key is not a tripwire ID |
| `unknown_override_field` | An override field is not in that tripwire's `spec` |
| `threshold_exceeds_window` | An errorSignature `threshold` is greater than its `windowRuns` |
| `container_kind_mismatch` | A `connectorInstance` or `jobInstance` component is not on `python`, or an `etlFlow` is not on `etl` |

| ID | Test | Pass condition |
| --- | --- | --- |
| D1-T1 | Contracts validate | Both contracts validate against `contracts/schema.json` (JSON Schema 2020-12) and load via `load_contract` |
| D1-T2 | Referential integrity | Every `tripwire.component` exists; every `${param}` in a spec exists in `parameters`; tripwire IDs unique; every `changePoint.overrides` key is a tripwire ID |
| D1-T3 | Enumerations | Component `kind` ∈ {connectorInstance, etlFlow, jobInstance}; tripwire `type` ∈ {cadence, synthetic, schema, errorSignature} |
| D1-T4 | Invalid contracts rejected | Each `fixtures/invalid/*.json` raises `ContractError` whose `code` equals its `_expectError`; at least 5 files covering missing component, unknown type, dangling parameter, duplicate tripwire ID, bad override |
| D1-T5 | Required tripwires | alert-ingestion has the four named tripwires (one per type); intel-sync has `dead-job` (cadence), `job-errors` (errorSignature) and `hollow-run` (schema on `jsonPayload.indicatorsSynced`, minimum 1, `consecutive` ≥ 2) |
| D1-T6 | Sample logs parse | Every line in `fixtures/logs/samples/*.jsonl` parses as `LogEntry` |
| D1-T7 | Faithful labels | In every sample and golden entry: `resource.type == "k8s_container"`; `namespace_name == "chronicle-soar"`; `container_name` ∈ {python, etl}; connector entries carry all four connector labels; job entries the three job labels; etl entries `correlation_id`; `insertId` unique per file |
| D1-T8 | Fleet config | Loads; environments alpha, bravo, charlie, canary with volumes 120/30/2/0; connector interval 60 s; job interval 300 s; job services only on alpha and bravo |
| D1-T9 | Scenario library complete | Every required scenario ID above exists for its service |
| D1-T10 | Scenario validity | Every scenario loads; fault types ∈ F1–F7 names; environments exist in the fleet; expected tripwire IDs exist in the service's contract; `clean` and `quiet-tenant` have empty `expected.trips` |
| D1-T11 | Golden fixtures paired | Each `fixtures/golden/X.jsonl` has `X.expected.json`; both parse; expected events have `kind`, `deployment`, `tripwire`, `atOffsetSeconds` |
| D1-T12 | Mutants present | At least 4 files in `fixtures/mutants/`; each schema-valid; each has `_mutation` |
| D1-T13 | Chronicle field specs | `specs/chronicle/{environments,integrationInstances,connectorInstances,connectorInstanceLogs,jobInstances,jobInstanceLogs}.yaml` exist, each with a `source:` URL on `docs.cloud.google.com/chronicle/docs/reference/rest/` and a `fields:` list equal to `mocks.md` §4.1 |
| D1-T14 | Logging decision recorded | `docs/decisions/logging-resource.md` exists and states `result: accepted` or `result: fallback` with the resource type to use |
| D1-T15 | Holdouts marked | Exactly the scenarios marked **holdout** above have `holdout: true`; every other scenario has `holdout: false`; each service has at least one holdout and at least one visible scenario with a fault |

**Out of scope:** any mock or evaluator behaviour.

---

## D2 — SRE feasibility spike

**Target:** Wed Oct 28 · **Owner:** Muse writes the spike; you write the fixture (or have Muse generate it in Session A and inspect it line by line) · **Depends on:** D0, D1

**Goal.** Find out today whether a Muse model diagnoses a credential Green Liar correctly inside the gated loop of `spec.md` §6.1: one tool call per turn, hypotheses first, evidence checked when it is recorded, and a diagnosis submitted through a gated tool. If it cannot, the plan changes now, not Saturday. D5 inherits whatever shape passes here.

**Files.** `fixtures/spike/green_liar/logs.jsonl` (three tenants, ~2 h; bravo shows WARNING runs with zero alerts, status LIVE), `fixtures/spike/green_liar/resources.json` (connector and integration instances; bravo's `executeTest` result `success: false`), `spikes/sre_spike.py` (the loop on the Responses API, plus minimal gates: 2–4 hypotheses with one alternative, the `insertId` exists, cited evidence IDs were recorded), `spikes/diagnosis_min.schema.json` (deployment, category, hypothesis, evidence IDs, action), `spikes/RESULTS.md`.

**Interface.** `python spikes/sre_spike.py --fixture <dir> --model {scripted|<model id>} [--runs N] [--gates on|off]`. Tools are stubs over the fixture files, with the names in `spec.md` §6.2 and §6.3; every tool takes `rationale`.

| ID | Test | Pass condition |
| --- | --- | --- |
| D2-T1 | Spike runs offline | With `--model scripted`, exits 0, prints a diagnosis valid against `diagnosis_min.schema.json`, and prints one trace line per turn (turn, rationale, tool, gate verdict) |
| D2-T2 | Stub tools are faithful | Each stub tool returns data drawn only from the fixture files; `query_logs` returns entries with their `insertId`s |
| D2-T3 | Gates refuse in the loop | A scripted run that records a nonexistent `insertId`, then submits citing an unrecorded evidence ID, gets both refusals back as tool results, and the run continues to an accepted submission |
| D2-T4 (live) | Live diagnosis | With a Muse model, `--runs 3`, gates on: at least 2 of 3 runs end with an accepted submission within 24 turns, one tool call per turn, naming deployment `bravo` and category `credential_failure` |
| D2-T5 (live) | Cost and protocol recorded | Each live run appends model, tokens in/out (reasoning and cached separately), turns, read calls, refusals by gate, latency and estimated cost to `spikes/RESULTS.md` |

**Decision rule.** If D2-T4 fails twice with prompt revisions, try the other model. If it still fails, simplify in this order and re-run after each step: drop `update_plan`; drop `set_hypotheses` (hypothesis coverage becomes report-only); pre-compute evidence into the incident. Record which shape passed in `spikes/RESULTS.md`; D5 starts from it.

---

## D3 — Mock SecOps (local)

**Target:** Thu Oct 29 · **Owner:** Muse · **Depends on:** D1

**Goal.** Everything in `mocks.md` runs in one process with a `SimClock`, emits faithful logs to a sink, and serves the Chronicle API surface with auth.

**Interfaces.**
```python
# fleetsre/clock.py
class Clock(Protocol): def now(self) -> datetime: ...
class SimClock(Clock): def __init__(self, start: datetime); def advance(self, seconds: float); def set(self, t: datetime)
class WallClock(Clock): ...

# fleetsre/ports.py
class LogSink(Protocol): def write(self, entry: LogEntry) -> None: ...
class MemorySink(LogSink): entries: list[LogEntry]
# fleetsre/adapters/jsonl.py
class JsonlSink(LogSink): def __init__(self, path)

# fleetsre/mock/world.py
class FaultSpec(BaseModel): type: str; environment: str; target: str | None = None; params: dict = {}
class World:
    def __init__(self, fleet: FleetConfig, seed: int, clock: Clock, sink: LogSink): ...
    def step_until(self, t: datetime) -> None
    def inject(self, fault: FaultSpec) -> str
    def clear(self, fault_id: str) -> None
    def plant_canary(self, environment: str) -> str          # returns providerIncidentId
    def snapshot(self) -> dict                                # all resource state, deterministic

# fleetsre/mock/chronicle_api.py
def create_app(world: World, *, chaos: bool = False, dev_auth: bool = False) -> FastAPI
```

Tests use FastAPI's `TestClient` with `X-Fleetsre-Identity` and `dev_auth=True`. "Doc field set" means the `fields:` list in `specs/chronicle/<resource>.yaml`.

| ID | Test | Pass condition |
| --- | --- | --- |
| D3-T1 | Connector instance shape | `GET` and `list` return objects whose keys equal the doc field set for connectorInstances (no extra, none missing) |
| D3-T2 | Resource names | Every returned `name` matches the path pattern for its resource in `mocks.md` §4.1 with the fixed prefix |
| D3-T3 | Enums and redaction | `status` ∈ {LIVE, NOT_LIVE}; every `parameters[].type` is a documented ParamType; `PASSWORD` values read `"********"` |
| D3-T4 | Patch semantics | `PATCH ...?updateMask=enabled` changes only `enabled`; patch without `integrationVersion` returns 400 `INVALID_ARGUMENT`; each successful patch increments `version` |
| D3-T5 | runOnDemand | Triggers exactly one extra run: one new `connector.run.completed` entry and new ConnectorLog records, without shifting the regular schedule |
| D3-T6 | setLogsCollection | After the call, DEBUG ConnectorLogs appear until `loggingEnabledUntilUnixMs`; none before or after |
| D3-T7 | Connector logs list | Records have the doc field set; `logLevel` is a documented value; newest first; `pageSize`/`pageToken` paginate without gaps or duplicates |
| D3-T8 | executeTest | Returns `success: true` normally and `success: false` with a message while F2 is active on that environment; `world.snapshot()` is identical before and after the call |
| D3-T9 | Job instances | Shape equals the doc field set; `list`, `get`, `patch`, `runOnDemand` work; patch with `intervalSeconds < 60` returns 400; job logs have `status` ∈ {SUCCESS, ERROR} |
| D3-T10 | Environments | `list` returns alpha, bravo, charlie, canary with the doc field set |
| D3-T11 | Errors | Unknown resource → 404 with body `{"error": {"code": 404, "message": <str>, "status": "NOT_FOUND"}}` |
| D3-T12 | Run cadence | Over 1 simulated hour with no faults, each connector instance emits 60 ± 1 `connector.run.completed` entries and each job instance 12 ± 1 `job.run.completed` entries |
| D3-T13 | Alert volume | Over 6 simulated hours (seeded), etl `alert.ingested` count per environment is within ±30% of `expectedAlertsPerHour × 6` for alpha and bravo, and between 2 and 30 for charlie |
| D3-T14 | Ingestion records | Every ingested alert yields exactly one etl entry with a unique `correlation_id`; with no faults, `jsonPayload.alert.priority` is always an int in {-1, 40, 60, 80, 100} |
| D3-T15 | Severity map | Sentinel `High/Medium/Low/Informational` map to 80/60/40/-1; an unmapped severity maps to `Unknown Severity Priority` when set, else passes through as the raw string |
| D3-T16 | Determinism | Two worlds with the same seed, fleet and fault schedule produce byte-identical JSONL over 2 simulated hours; a different seed produces a different stream |
| D3-T17 | F1 stall | After injection, the target instance emits no python entries and no ConnectorLogs; `status` stays LIVE; siblings unchanged; an `enabled` false→true transition clears it within 2 intervals |
| D3-T18 | F2 credential expiry | Target environment: runs continue on schedule, each with a WARNING entry and a WARNING ConnectorLog, zero etl entries; `status` stays LIVE; other environments unaffected |
| D3-T19 | F3 severity drift | Target environment: etl volume within ±30% of normal; some `priority` values are strings; patching `Unknown Severity Priority` to `100` makes new entries ints again |
| D3-T20 | F4 hollow success | `Lookback Hours` becomes `0`; job runs report SUCCESS with `indicatorsSynced == 0`; patching it back to `24` restores non-zero syncs |
| D3-T21 | F5 job error | Job runs end ERROR (`lastRunStatus`, job log `status`, python entry severity ERROR); every run still ends with a `job.run.completed` entry with `status: ERROR` |
| D3-T22 | F6 flap | With `rate` r, the fraction of ERROR runs over 100 runs is r ± 0.1; every run, ERROR or not, ends with `connector.run.completed` and still ingests its alerts |
| D3-T23 | F7 job stall | No job runs; `nextScheduledRunTime` goes stale; an `enabled` false→true transition clears it |
| D3-T24 | Clearing faults | `clear()` restores normal behaviour within 2 intervals for every fault type |
| D3-T25 | Canary | `plant_canary(env)` produces an etl entry with `alert.canary == true` within 2 intervals; never appears while F2 is active on that environment |
| D3-T26 | Chaos gating | With `chaos=False` every `/_chaos` route returns 404; with `chaos=True` the routes in `mocks.md` §7 work |
| D3-T27 | Authorization | `obs-reader` gets 403 `PERMISSION_DENIED` on every create, patch, `runOnDemand` and `setLogsCollection`; `deployer` succeeds; a missing identity gets 401; `executeTest` is allowed for both |
| D3-T28 | Log validity | Every entry written in a 2-hour run with all faults cycled validates against `LogEntry` and satisfies the label rules of D1-T7 |
| D3-T29 | JSONL sink | One entry per line, append-only, re-readable with `read_jsonl` |

**Out of scope:** evaluator, GCP, UI.

---

## D4 — Evaluator and chaos harness

**Target:** Thu Oct 29 · **Owner:** Muse · **Depends on:** D1, D3

**Goal.** Pure tripwire functions, a stateful evaluator, a canary scheduler, and a harness that scores a contract against the scenario library.

**Interfaces.**
```python
# fleetsre/evaluator/tripwires.py   (pure: no I/O, no clock reads)
def cadence_due(last_event_at: datetime | None, armed_at: datetime, now: datetime, interval_s: int, max_silent: int) -> bool
def schema_violation(entry: LogEntry, spec: SchemaSpec) -> str | None          # reason or None
def error_signature(recent_run_severities: Sequence[str], spec: ErrorSigSpec) -> bool
def canary_missed(planted_at: datetime, seen_at: datetime | None, now: datetime, deadline_s: int) -> bool

# fleetsre/evaluator/engine.py
@dataclass(frozen=True)
class TripEvent: kind: Literal["trip", "recovery"]; deployment: str; tripwire: str; at: datetime; reason: str; evidence: tuple[str, ...]
class Evaluator:
    def __init__(self, contracts: list[Contract], deployments: list[Deployment], clock: Clock): ...
    def ingest(self, entry: LogEntry) -> list[TripEvent]
    def tick(self) -> list[TripEvent]
    def canary_planted(self, deployment: str, provider_incident_id: str, at: datetime) -> None
    def enter_change_point(self, deployment: str, component: str, *, duration_s: int, overrides: dict) -> None
    def status(self) -> dict[str, dict[str, Literal["ok", "tripped"]]]

# fleetsre/evaluator/canary.py
class CanaryScheduler: def __init__(self, world_or_planter, evaluator, contracts, clock); def tick(self) -> None

# fleetsre/harness/replay.py, scorecard.py
def run_scenario(contract: Contract, scenario: Scenario, *, tick_seconds: int = 15) -> ScenarioResult
def scorecard(contract: Contract, scenarios: list[Scenario]) -> Scorecard
@dataclass class ScenarioResult: scenario_id; expected; actual; latencies; missing; unexpected; passed: bool
@dataclass class Scorecard: results: list[ScenarioResult]; passed: bool; expected_hit_rate: float; false_positives: int
```
Deployment IDs are `<service>/<environment>`.

| ID | Test | Pass condition |
| --- | --- | --- |
| D4-T1 | Cadence boundaries | `cadence_due` is False at `max_silent × interval − 1 s` after the last event and True at `+1 s`; with no events yet, the window counts from `armed_at` |
| D4-T2 | Quiet tenant | The `quiet-tenant` scenario (charlie, 2 alerts/h, 6 h) produces zero trips with the reference contract |
| D4-T3 | Canary boundaries | `canary_missed` is False when seen at `deadline − 1 s`, True at `deadline + 1 s` unseen |
| D4-T4 | Schema detection | `schema_violation` returns a reason for a missing field, a wrong type and an out-of-enum value, and None for a valid entry; with `consecutive: K`, the evaluator trips only on the K-th consecutive violation |
| D4-T5 | Error signature | Trips when WARNING-or-worse runs in the last `windowRuns` ≥ `threshold`; a single warning does not trip |
| D4-T6 | Dedupe | A tripped (deployment, tripwire) emits no further trip events until it has recovered |
| D4-T7 | Recovery | Cadence recovers on the next heartbeat; canary on the next successful canary; schema after `recoverAfter` consecutive valid entries (default `consecutive`); errorSignature when the window drops below threshold, where a run is one `runId` and its severity is the worst entry in it |
| D4-T8 | Isolation | Property test: a fault on one environment, placed at random times across 50 seeded runs, never trips another environment's tripwires |
| D4-T9 | Purity and replay | Tripwire functions perform no I/O (checked by import-linter rule and a monkeypatched `open`/socket); replaying the same stream and tick times twice yields identical event lists |
| D4-T10 | Out-of-order tolerance | Entries arriving up to 60 s out of timestamp order cause no false trips |
| D4-T11 | Golden fixtures | For each `fixtures/golden/X.jsonl`, replayed with the `deployments`, `t0`, `durationSeconds`, `tickSeconds` and `canariesPlanted` that `X.expected.json` declares (a canary is seen when an `alert.ingested` entry has `alert.canary: true` and `alert.ticket_id` equal to its `providerIncidentId`), the evaluator's events equal its `events` exactly (kind, deployment, tripwire, offset ± tick) |
| D4-T12 | Reference scorecard | The reference contracts score `passed == True`, 100% expected hits, 0 false positives over their full scenario libraries |
| D4-T13 | Mutants fail | Every contract in `fixtures/mutants/` scores `passed == False` |
| D4-T14 | Latency reporting | Every expected trip in a passing scenario reports a latency ≤ its `withinSeconds` |
| D4-T15 | Change-point mode | `enter_change_point` applies the overrides (e.g. `maxSilentIntervals: 2` trips after two silent intervals), re-arms cadence from the moment it is entered (a heartbeat from before the change point never counts) and reverts after `duration_s` |
| D4-T16 | Canary scheduler | Plants one canary per `everyMinutes` per deployment with a synthetic tripwire and calls `canary_planted` for each |
| D4-T17 | Trip evidence | Every trip event carries at least one `insertId` of a relevant entry (the last heartbeat, the violating entry, or the warning entries), except canary misses, which carry the planted incident ID in `reason` |
| D4-T18 | Speed | The full scenario library for both services replays in under 30 s on a laptop |

**Out of scope:** incidents, model calls, GCP.

---

## D5 — SRE investigator

**Target:** Fri Oct 30 · **Owner:** Muse; **you** write the SRE system prompt (`prompts/investigate.md`, committed in the tests PR, protected from then on; start from what worked in the D2 spike; it must state the loop rules of `spec.md` §6.1 and the gates of §6.3 and §6.5) · **Depends on:** D3, D4, D2 results

**Goal.** A gated, budgeted investigation loop (`spec.md` §6.1–§6.6) that ends in an accepted diagnosis or a NEEDS_INPUT pause, with every turn saved and resumable.

**Interfaces.**
```python
# fleetsre/sre/schemas.py
class Hypothesis(BaseModel): id: str; statement: str; wouldConfirm: str; alternative: bool
class Evidence(BaseModel): id: str; hypothesis: str; stance: Literal["supports", "refutes"]; kind: Literal["log", "toolResult"]
    insertId: str | None; callId: str | None; field: str | None; value: Any; why: str; recordedAtTurn: int
class Diagnosis(BaseModel): ...       # exactly spec.md §6.5
class Incident(BaseModel): id; deployment; tripwire; opened_at; trip_event: TripEvent

# fleetsre/sre/session.py   (one investigation; saved after every turn)
class Session(BaseModel): incident_id; turn; read_calls; tokens; hypotheses; evidence; calls; refusals; status; reason; resumed_at_turns: list[int]
class SessionStore: def save(self, s: Session) -> None; def load(self, incident_id: str) -> Session

# fleetsre/sre/gates.py   (pure: inputs in, list of problems out)
def check_hypotheses(items: list[dict]) -> list[str]
def check_evidence(args: dict, session: Session, log_query: LogQuery, incident: Incident) -> list[str]
def check_diagnosis(d: dict, session: Session, incident: Incident, contract: Contract) -> list[str]

# fleetsre/sre/tools.py
class ToolSet:
    def __init__(self, log_query: LogQuery, chronicle: ChronicleClient, state: StateStore, registry: Registry, *, verification: bool = True): ...
    def specs(self) -> list[dict]                       # read tools (spec.md §6.2) + working tools (§6.3)
    def call(self, name: str, args: dict, session: Session) -> dict   # refusals return {"error", "problems"}; never raises

# fleetsre/sre/gateway.py
class RoleConfig(BaseModel): model: str; reasoning_effort: str
class GatewayConfig(BaseModel): roles: dict[str, RoleConfig]; base_url: str; max_retries: int = 5
class ModelGateway:
    def __init__(self, config: GatewayConfig, transport: Transport): ...
    def respond(self, role: str, input_items: list[dict], tools: list[dict] | None) -> ModelResponse   # Responses API, store=False
class ScriptedTransport(Transport): def __init__(self, responses: list[dict])   # tests and recorded replay

# fleetsre/sre/investigator.py
@dataclass class InvestigationResult: diagnosis: Diagnosis | None; status: Literal["submitted", "needs_input"]; reason: str
    transcript_id: str; turns: int; read_calls: int; tokens: dict; refusals: dict[str, int]
def investigate(incident: Incident, tools: ToolSet, gateway: ModelGateway, *, max_turns: int = 24, max_read_calls: int = 12, max_tokens: int = 400_000) -> InvestigationResult
def resume(incident: Incident, tools: ToolSet, gateway: ModelGateway, *, message: str, extra_turns: int = 8) -> InvestigationResult
```
`status="submitted"` means an accepted `propose` or `none`; its `reason` is the action type. `status="needs_input"` carries one of the reasons in `spec.md` §6.1 (`escalated`, `refused`, `budget`, `token_budget`, `stuck`, `model_unavailable`); for `escalated`, `diagnosis` is set. `make record FAULT=<id>` runs one live investigation on its seeded world and writes it to `fixtures/recorded/<id>.json` for your review.

| ID | Test | Pass condition |
| --- | --- | --- |
| D5-T1 | Tool set complete | `ToolSet.specs()` lists exactly the eight read tools in `spec.md` §6.2 and the four working tools in §6.3; every schema has `additionalProperties: false` and a required `rationale`; each read tool returns schema-valid output against a seeded local world |
| D5-T2 | Tools are read-only | The ToolSet's Chronicle client uses the `obs-reader` identity; no tool can invoke a write method (attempt via a crafted call → error, world snapshot unchanged) |
| D5-T3 | Loop shape | Every model request is a Responses call with `store=False`, `parallel_tool_calls=False` and encrypted reasoning included, carrying the full history; only the first tool call in a response runs; a response with no tool call gets the plain-text message from `spec.md` §6.1 and runs no tool |
| D5-T4 | Order gate | Before `set_hypotheses` is accepted, `record_evidence` and `submit_diagnosis` return an error result (no exception) and the loop continues |
| D5-T5 | Hypotheses gate | Fewer than 2 or more than 4 items, or none marked `alternative`, is refused with problems; a valid set is accepted; replacing the set drops evidence for removed hypotheses |
| D5-T6 | Evidence gate | Refused, each with a problem naming why: an `insertId` that does not exist; one from a deployment that is neither the incident's nor a sibling queried in this session; a `callId` not in this session; a `field`/`value` that does not match that call's result; an unknown hypothesis; a fourth item for one hypothesis. Accepted evidence gets the next `E<n>` ID |
| D5-T7 | Diagnosis gate | Each rule in `spec.md` §6.5 refuses a diagnosis that breaks it, with a problem naming it: schema invalid; an unrecorded evidence ID; hypothesis coverage missing; each category's evidence rule unmet (one row per category); an action outside the allowlist or an `updateMask` outside its fields; a resource that is not the incident's component; `none` without evidence recorded after the last resume |
| D5-T8 | Refusal cap | Three refused submissions → `status="needs_input"`, `reason="refused"`; every refusal and its problems are in the transcript |
| D5-T9 | Budgets | The model is told on turn `max_turns` that it is the last turn; with no accepted submission the result is `needs_input`/`budget`. The 13th read call is refused with an error and the loop continues. Exceeding `max_tokens` → `needs_input`/`token_budget` |
| D5-T10 | Stuck | The same tool with the same canonical arguments (keys sorted, `rationale` excluded) a third time → `needs_input`/`stuck` |
| D5-T11 | Import boundary | import-linter contract: `fleetsre.sre` imports nothing from `google`, `fleetsre.adapters`, `fleetsre.mock` |
| D5-T12 | Transcript and per-turn save | After every turn the session and transcript are saved: each output item, tool call, result, gate verdict, token counts (reasoning and cached separately) and estimated cost; retrievable by `transcript_id`. An investigation killed after turn k and resumed continues at turn k+1 without re-running turns 1..k |
| D5-T13 | Resume | `resume` continues the same session: hypotheses and evidence are kept, `message` is the next input, `extra_turns` extends the turn budget, and evidence recorded after the resume carries its turn |
| D5-T14 | Backoff | With a transport returning 429 three times then success, the call succeeds after exponential delays (asserted on a fake sleeper); after `max_retries` 429s the result is `needs_input`/`model_unavailable` |
| D5-T15 | Role config | Changing `GatewayConfig.roles["investigate"]` changes the model ID and the reasoning effort sent in requests, with no code change |
| D5-T16 | Scripted end-to-end | With scripted model responses for each of F1–F5 and F7 (from `fixtures/scripted/`), every submission passes the gates and `investigate` returns the right-answer category and action from `mocks.md` §6; F2 and F5 end `needs_input`/`escalated` |
| D5-T17 | Recorded replay | Each `fixtures/recorded/*.json` replays offline through `ScriptedTransport` on its seeded world with identical tool results, gate verdicts and final diagnosis; any difference fails. At least one recording exists |
| D5-T18 | Gates off | With `ToolSet(verification=False)` the gates never refuse, and each verdict they would have given is recorded in the transcript as `wouldRefuse` |
| D5-T19 (live) | Live eval | For F1, F2, F3, F4, F5 and F7 on a seeded world, 3 runs each, gates on: at least 2 of 3 per fault end accepted (or `escalated` for F2 and F5) with the right deployment, component and category, within budgets. Results written to `evals/d5-<date>.json` |
| D5-T20 (live) | Reported, not gating | In the D5-T19 eval file: refusals by gate per run; for F2, the share of runs that call `compare_siblings` or `test_integration_instance`; and a gates-off run of the same seeds (D5-T18) with the number of submissions the gates would have refused, by gate |

`fixtures/scripted/` (Session A, you review): scripted model response sequences per fault, including the tool calls a good investigator makes. `fixtures/recorded/` (you): after D5-T19 passes, record 2–3 live runs with `make record` and review them; the implementation PR merges once at least one is committed.

**Out of scope:** approvals, executor, verification, authoring, the NEEDS_INPUT resume rules (D6).

---

## D6 — Fix loop, authoring and amendment

**Target:** Sat Oct 31 · **Owner:** Muse; **you** write the authoring prompt (`prompts/author.md`, committed in the tests PR) · **Depends on:** D4, D5

**Goal.** The full loop of `spec.md` §3: incident lifecycle with NEEDS_INPUT pauses, approval or rejection, journaled apply, verify, postmortem; contract authoring against the scorecard with held-out scenarios; amendments that apply fleet-wide.

**Interfaces.**
```python
# fleetsre/control/incidents.py
class IncidentState(StrEnum): OPEN; INVESTIGATING; NEEDS_INPUT; AWAITING_APPROVAL; APPLYING; VERIFYING; RESOLVED; ESCALATED
class IncidentManager:
    def __init__(self, evaluator, investigator, approvals, executor, verifier, state: StateStore, clock): ...   # raises if the investigator's ToolSet has verification=False
    def on_trip(self, event: TripEvent) -> Incident
    def tick(self) -> None                         # drives transitions; resumes NEEDS_INPUT incidents whose tripwire state changed since they paused
    def reply(self, incident_id: str, message: str, *, by: str, extra_turns: int = 0) -> None
    def close(self, incident_id: str, *, by: str, note: str) -> None     # NEEDS_INPUT -> ESCALATED
# fleetsre/control/approvals.py
class ApprovalStore:
    def approve(self, incident_id: str, proposal_hash: str, approver: str) -> Approval
    def reject(self, incident_id: str, proposal_hash: str, approver: str, notes: str) -> Rejection
# fleetsre/control/journal.py
class Journal: def append(self, record: dict) -> None; def records(self) -> list[dict]          # append-only
# fleetsre/control/executor.py
class NotApproved(Exception): ...;  class Conflict(Exception): ...
class Executor:
    def __init__(self, chronicle: ChronicleClient, approvals: ApprovalStore, journal: Journal): ...   # deployer identity
    def apply(self, incident: Incident, diagnosis: Diagnosis) -> ApplyResult
    def recover(self) -> list[ApplyResult]         # reconciles intents that have no terminal record
# fleetsre/control/verify.py
class Verifier: def start(self, incident, diagnosis) -> None; def check(self) -> Literal["pending", "verified", "failed"]
# fleetsre/authoring/loop.py
def author_contract(brief: ServiceBrief, samples: list[LogEntry], scenarios: list[Scenario], gateway: ModelGateway, *, max_revisions: int = 3) -> AuthoringResult
# fleetsre/authoring/amend.py
def propose_amendment(incident: Incident, contract: Contract, gateway: ModelGateway, scenarios: list[Scenario]) -> AmendmentResult
# fleetsre/registry.py
class Registry: def apply_contract(self, contract: Contract, approval: Approval) -> None   # reloads evaluator for all deployments of the service
```
Concurrency: when a proposal is created, the platform records the target resource's `version` (connector) or `updateTime` (job); the executor compares at apply time. Resume rules: a NEEDS_INPUT incident resumes only through `reply` or through `tick` seeing its tripwire state change; anything else makes no model call (`spec.md` §6.1).

| ID | Test | Pass condition |
| --- | --- | --- |
| D6-T1 | Approval required | `Executor.apply` without an approval raises `NotApproved`; an approval for a different proposal hash also raises it |
| D6-T2 | Concurrency | If the resource's `version`/`updateTime` changed between proposal and apply, `apply` raises `Conflict` and the incident returns to INVESTIGATING |
| D6-T3 | Deployer identity | The executor's client uses `deployer`; the same call with `obs-reader` gets 403 |
| D6-T4 | Restart macro | `connectorInstances.restart` and `jobInstances.restart` issue `enabled=false` then `enabled=true` patches in order |
| D6-T5 | Verification | After apply, the change-point window is armed with the contract's overrides; the diagnosis's `expectedVerification` met → RESOLVED; not met within `seconds` → back to INVESTIGATING; a second failure → ESCALATED |
| D6-T6 | E2E F1 | Scripted model, SimClock: inject F1 → trip → investigation → proposal → approval → restart → verification → RESOLVED |
| D6-T7 | E2E F2 | Inject F2 → trip → investigation → NEEDS_INPUT (`escalated`, category `credential_failure`). Advancing the clock 1 h makes no model call, and `tick` does not resume while the fault is active and nobody replied. Clearing F2 ("mark rotated") → the tripwire recovers → `tick` resumes the same session with that change as its message → the SRE submits action `none` → verification → RESOLVED. No write call is made at any point |
| D6-T8 | E2E F3 | Inject F3 → schema trip → parameter patch → approval → priorities ints again → RESOLVED |
| D6-T9 | E2E F4 | Inject F4 → hollow-run trip → `Lookback Hours` patch → RESOLVED |
| D6-T10 | Postmortem | Every RESOLVED or ESCALATED incident has a postmortem with timeline (including NEEDS_INPUT pauses and what resumed them), root cause category, action taken and transcript IDs |
| D6-T11 | Authoring loop | Scripted model: v1 fails the scorecard → the next prompt contains the scorecard's missing/unexpected list for visible scenarios → v2 passes → result `passed=True`, 2 scorecards recorded; with a model that never passes, stops after `max_revisions` with `passed=False` |
| D6-T12 | Authoring hides the exam | The authoring prompts never contain scenario `expected` blocks (asserted on the prompt text); they contain scorecard results only |
| D6-T13 | Authored output valid | Every authored contract validates via `load_contract` before scoring; invalid output counts as a failed revision |
| D6-T14 | Amendment | From an incident whose detection latency exceeded `withinSeconds` (a scripted slow contract), `propose_amendment` returns a contract that scores higher; it is not applied until approved; once approved, `Registry.apply_contract` reloads every deployment of that service |
| D6-T15 (live) | Live authoring eval | With the configured authoring model and the alert-ingestion brief and samples, at least 2 of 3 runs reach a passing scorecard, holdouts included, within 3 revisions. Results to `evals/d6-<date>.json` |
| D6-T16 | Needs-input handling | `reply` resumes the session with the reply as the next message; `reply` with `extra_turns` resumes a `budget` pause; `close` moves the incident to ESCALATED with a postmortem; a resume attempt with nothing changed makes no model call |
| D6-T17 | Reject with notes | `reject` returns the incident to INVESTIGATING with the notes as the next message; the revised proposal has a new hash; the earlier approval cannot apply it |
| D6-T18 | Executor journal | For every apply the journal holds `intent` (approval ID, proposal hash, resource version) before the API call, then `started`, then `terminal`; no write call happens without a prior `intent` |
| D6-T19 | Crash-safe apply | An executor stopped after `intent` and before `terminal`, then `recover()`: if the resource already shows the change, it is marked terminal without an API call; otherwise it is applied once. In both cases the patch lands exactly once |
| D6-T20 | Gates always on | `IncidentManager` raises when given an investigator whose ToolSet has `verification=False` |
| D6-T21 | Held-out scenarios | Authoring and amendment prompts never contain a holdout scenario's ID, fault schedule or result; `passed` requires the holdouts to pass; when only a holdout fails, the feedback says a held-out scenario failed and nothing more |

**Out of scope:** UI, GCP.

---

## D7 — GCP adapters and IAM

**Target:** Sun Nov 1 · **Owner:** Muse with you (your credentials, your approvals of every gcloud/Terraform step) · **Depends on:** D3–D6

**Goal.** The same system runs on GCP: mock logs to Cloud Logging, sink to Pub/Sub, push to the evaluator, Firestore state, Cloud Run services, three-plus identities with tests that prove the air gap.

**Interfaces / files.** `fleetsre/adapters/cloud_logging.py` (`CloudLoggingSink`, `CloudLoggingQuery`), `fleetsre/adapters/pubsub_push.py` (`create_push_app(evaluator, *, audience, allowed_sa)`), `fleetsre/adapters/firestore_state.py`, `fleetsre/adapters/chronicle_http.py` (ID-token client), `infra/` (Terraform or gcloud scripts), `infra/expected_permissions.yaml`, `make infra-up`, `make infra-down`.

| ID | Test | Pass condition |
| --- | --- | --- |
| D7-T1 (gcp) | Cloud Logging write/read | Entries written by `CloudLoggingSink` are returned by `CloudLoggingQuery` with filter `resource.labels.namespace_name="chronicle-soar"`, with resource and entry labels intact (or the fallback resource from D1-T14) |
| D7-T2 (gcp) | Sink filter | Of one matching and one non-matching entry, only the matching one arrives on the subscription |
| D7-T3 | Push handler auth | Offline: the push app rejects requests without a valid OIDC token for the expected audience and service account (401/403), using a fake verifier |
| D7-T4 | Push idempotency | Offline: redelivering the same message (same `insertId`) produces no duplicate evaluator events |
| D7-T5 | Firestore state | Offline against the Firestore emulator (`make emulators`): deployments, tripwire state, incidents, investigation sessions, approvals, the executor journal and transcripts round-trip; a restarted evaluator resumes without re-emitting trips; a restarted executor reconciles open intents as in D6-T19 |
| D7-T6 | Tick auth | Offline: the tick endpoint accepts only OIDC tokens from the scheduler service account |
| D7-T7 (gcp) | IAM positive | Each identity completes its own path: mock-soar writes logs, obs-reader reads logs and pulls from the subscription and reads the mock API, deployer patches via the mock API |
| D7-T8 (gcp) | IAM negative | obs-reader: mock write → 403, `logging.logEntries.create` → denied; deployer: subscription pull → denied; mock-soar: log read → denied; the SRE runtime has no credentials of its own |
| D7-T9 (gcp) | Exact permissions | `testIamPermissions` for each identity on the project and on the subscription returns exactly the set in `infra/expected_permissions.yaml` |
| D7-T10 (gcp) | No keys | No service account has user-managed keys |
| D7-T11 (gcp) | Reproducible infra | `make infra-up` on a clean project creates everything; a second run changes nothing; `make infra-down` removes it |
| D7-T12 (gcp) | Cloud parity | Scenario `stall` run through mock → Cloud Logging → Pub/Sub → evaluator produces the same trip as the local harness, with latency ≤ `withinSeconds` + 120 s |

**Out of scope:** UI.

---

## D8 — UI, live chaos loop and deploy

**Target:** Mon Nov 2 · **Owner:** Muse; keep the UI minimal · **Depends on:** D6, D7

**Goal.** A judge can open a URL and watch faults trip, get investigated turn by turn and recover without touching anything; you can approve, reject and answer handoffs in admin mode; `make demo` reproduces it locally.

**Interfaces.** `fleetsre/web/app.py` `create_app(mode: Literal["admin", "public"], ...)`; `fleetsre/live/chaos_loop.py` `ChaosLoop(world, schedule, clock).tick()`.

| ID | Test | Pass condition |
| --- | --- | --- |
| D8-T1 | Fleet grid | `GET /api/fleet` returns services × deployments × components with health from `Evaluator.status()`; the HTML grid renders every deployment |
| D8-T2 | Incident view | `GET /incidents/{id}` shows hypotheses, recorded evidence (resolved from `insertId`s and call IDs), category, the patch diff, verification state and the postmortem |
| D8-T3 | Approve and reject (admin) | In admin mode, approving creates an approval for the current proposal hash and moves the incident to APPLYING; rejecting with notes calls `ApprovalStore.reject`; requests without the admin credential get 403 |
| D8-T4 | Public mode | In public mode: no `/_chaos` routes, no approve, reject or reply routes, a cap of N model calls per hour (beyond it, cached diagnoses are served). Proposals from the chaos loop are approved by a demo auto-approver, and when an F2 incident reaches NEEDS_INPUT the chaos loop clears the fault. Every such action is labelled ("demo auto-approval", "demo: credential rotated") on the page and in the incident record |
| D8-T5 | Chaos loop | Over a simulated 2 hours, the loop cycles F1, F2, F3, F4 on schedule and clears each after its incident closes (F2: when it reaches NEEDS_INPUT, per D8-T4); no unhandled exceptions |
| D8-T6 | Local demo | `make demo` from a clean clone starts mock, evaluator, SRE (scripted model unless a key is set) and UI; the grid shows a trip within 5 minutes |
| D8-T7 (gcp) | Unattended hour | The public instance runs one hour unattended: at least 3 incidents opened and closed, no 5xx in logs, Meta spend within the configured cap |
| D8-T8 | README | Contains: what it is, how to run locally, architecture, where each gate sits and what it refuses, "Built during the hackathon" (list of components with deliverable IDs), faithful-vs-invented table from `mocks.md` §9, and links to the live instance and video |
| D8-T9 | Turn timeline | The incident page has one card per turn with Think (the call's `rationale`), Act (tool and arguments) and Observe (the result, or the gate's refusal and problems, coloured by outcome); expanding a card shows the exact input the model saw that turn; a header shows turns, read calls, tokens (reasoning and cached) and refusals by gate |
| D8-T10 | Handoff card | A NEEDS_INPUT incident shows its reason, hypotheses, recorded evidence and last refusal. Admin mode offers reply, grant turns and close, wired to `IncidentManager.reply` and `close`; public mode shows the card without actions |
| D8-T11 | Step mode (admin) | With step mode on, an investigation pauses before each turn until "Next turn" or "Run to end"; with it off, there is no pause |

**Out of scope:** anything new. After D8, only fixes.
