# D7 pins

Interfaces and semantics for D7 (lite), GCP adapters and the IAM air gap: `deliverables.md` §D7 rows T1, T7, T8 and T10. Written before the tests PR. This file wins over the §D7 interface block where they differ.

## Scope

- **In:**
  - a Cloud Logging sink and query adapter;
  - keyless identities by impersonation;
  - one gcloud script that creates the three service accounts, the log sink, the topic and the subscription, and their bindings;
  - live tests that prove each identity's path and the air gap.
- **Out:** Pub/Sub push, Firestore, Cloud Run, Cloud Scheduler, the ID-token Chronicle client, `infra/expected_permissions.yaml`, and reproducible-infra tests (all cut, `deliverables.md` §Cut).
- **The mock API stays local under dev auth.** D3-T27 and D6-T3 already prove "obs-reader reads the mock API", "deployer patches via the mock API" and "obs-reader: mock write → 403". T7 and T8 assert the mock clauses by calling the local app with dev auth, so each row reads complete. They add the GCP clauses.
- **Who runs what.** Muse writes the adapters, the infra script and the tests. It never runs gcloud or any live GCP call, and gcloud stays denied in its approval policy. Rixon runs `make infra-up`, `make test-gcp` and `make infra-down`.

## Names

All in project `$FLEETSRE_GCP_PROJECT` (`fleet-sre-1029-9324`):

| Resource | Name |
| --- | --- |
| Service accounts | `mock-soar`, `obs-reader`, `deployer` (`<id>@<project>.iam.gserviceaccount.com`) |
| Log name | `projects/<project>/logs/chronicle-soar` |
| Topic | `fleet-sre-logs` |
| Subscription | `fleet-sre-logs-obs` (pull, 10-minute retention is fine) |
| Log sink | `fleet-sre-soar`, destination `pubsub.googleapis.com/projects/<project>/topics/fleet-sre-logs`, filter `resource.type="k8s_container" AND resource.labels.namespace_name="chronicle-soar"` |

## Grants (spec §7)

| Identity | Grants | Must be denied |
| --- | --- | --- |
| `mock-soar` | `roles/logging.logWriter` on the project | log read (`logging.logEntries.list`) |
| `obs-reader` | `roles/logging.viewer` on the project; `roles/pubsub.subscriber` on the subscription only | log write (`logging.logEntries.create`); mock API writes (dev auth) |
| `deployer` | none in GCP (its path is the mock API, under dev auth) | subscription pull |
| Sink writer identity | `roles/pubsub.publisher` on the topic | — |
| Rixon's user account | `roles/iam.serviceAccountTokenCreator` on each of the three service accounts | — |

No service account gets a key. The user account impersonates each one.

## Interfaces

1. **`fleetsre/adapters/gcp_auth.py`.**
   - `service_account_email(name: str, project: str) -> str` returns `f"{name}@{project}.iam.gserviceaccount.com"`.
   - `impersonated(name: str, project: str, *, scopes: list[str] | None = None) -> google.auth.credentials.Credentials` builds `google.auth.impersonated_credentials.Credentials` from Application Default Credentials.
     - The default scope is `https://www.googleapis.com/auth/cloud-platform` and the lifetime is 3600 s.
     - No key file is ever read.
     - It does not refresh the credentials; callers do (pin 5).

2. **`fleetsre/adapters/cloud_logging.py`.**
   - `to_cloud_entry(entry: LogEntry, project: str) -> dict` converts a `LogEntry` to the Cloud Logging API's JSON `LogEntry`.
     - `logName` is `projects/<project>/logs/chronicle-soar`.
     - `resource.labels.project_id` is `<project>`, whatever the entry held (docs/decisions/logging-resource.md).
     - `insertId`, `timestamp`, `severity`, `labels` and `jsonPayload` are copied.
   - `from_cloud_entry(data: dict) -> LogEntry` converts back, and is pure.
   - `CloudLoggingSink(project, *, credentials=None)` implements `LogSink`. `write(entry)` sends one entry through `google.cloud.logging_v2` (`entries.write`), with a `partial_success` of false.
   - `CloudLoggingQuery(project, *, credentials=None, extra_filter: str = "")` implements `LogQuery` (d5-pins 1).
     - `entries(start, end)` lists entries with the base filter `resource.type="k8s_container" AND resource.labels.namespace_name="chronicle-soar"`, plus `timestamp>="<start>" AND timestamp<"<end>"`, plus `extra_filter` if set. It returns them oldest first.
     - `get(insert_id)` lists with the base filter and `insertId="<id>"` over the last 24 h, and returns the entry or `None`.
   - `build_filter(start, end, extra="") -> str` and `build_get_filter(insert_id) -> str` are public, so unit tests can check the filter text.
   - Neither class adds retries of its own. A `google.api_core.exceptions.PermissionDenied` propagates.

3. **`infra/gcp.sh`**, with `make infra-up` (`infra/gcp.sh up`) and `make infra-down` (`infra/gcp.sh down`).
   - It reads `FLEETSRE_GCP_PROJECT` and fails at once if it is unset.
   - `up`:
     - enables `logging`, `pubsub`, `iam` and `iamcredentials`;
     - creates every resource in §Names and every grant in §Grants;
     - gives the token-creator grant to the account from `gcloud config get-value account`;
     - reads the sink's writer identity back with `gcloud logging sinks describe --format='value(writerIdentity)'`.
   - `up` is idempotent. An existing resource is left as it is, and a binding is re-added (gcloud's add is idempotent).
   - **Propagation.** A binding whose member is a just-created service account can fail with "does not exist" for up to about a minute. Each `add-iam-policy-binding` call retries up to 6 times, 10 s apart, before the script fails.
   - `up` ends by printing: `IAM changes can take several minutes to apply. Wait ~5 minutes before make test-gcp.`
   - `down` removes, in order:
     1. the project-level bindings for the three service accounts, so the policy keeps no `deleted:serviceAccount:` members;
     2. the sink;
     3. the subscription;
     4. the topic;
     5. the three service accounts.

     It ignores resources and bindings that are already gone.
   - The script prints each gcloud command before it runs it (`set -x` or an echo), so Rixon sees every step.

4. **`pyproject.toml`, in Session A.**
   - `google-cloud-logging`, `google-cloud-pubsub`, `google-cloud-iam` and `google-auth` join the main dependencies. Session A adds them and reinstalls (`pip install -e .`), so the tests collect and type-check, and CI installs them.
   - If mypy reports missing stubs for a `google.*` module, add a `[[tool.mypy.overrides]]` with `ignore_missing_imports = true` for that module only.
   - The import-linter contract that keeps `google` out of `fleetsre.sre` is unchanged.
   - The acceptance tests import Google packages inside the test functions.

## Semantics

5. **Live tests.**
   - They are marked `gcp`, deselected by `make check`, and run with `make test-gcp`.
   - Each test skips with a message if `FLEETSRE_GCP_PROJECT` is unset.
   - **Stub first.** After the skip check, each test's first call is to a D7 interface (`impersonated` or `service_account_email`), before any Google package is imported or called. So, with stubs in place, every test fails with `NotImplementedError` and needs neither infra nor credentials.
   - **Refresh first.** Every impersonated credential is refreshed explicitly with `creds.refresh(google.auth.transport.requests.Request())` before the first API call that uses it. A `google.auth.exceptions.RefreshError` is retried every 2 s until a 120 s deadline, because the token-creator grant takes time to apply. A failure after the deadline fails the test with the last error. Over gRPC, a failed impersonation would otherwise surface as `ServiceUnavailable` ("Getting metadata from plugin failed"), not as `PermissionDenied`.
   - They poll real time with a deadline: 90 s for a log read-back, 120 s for a Pub/Sub pull, 2 s between tries. This is an exception to "no sleep in tests", which governs `SimClock` tests.
   - **Tagging.** Each run tags its entries with a fresh `jsonPayload.runId = "d7-<uuid4 hex>"` and insertIds `d7-<uuid>-<n>`. Its queries filter on that tag, so runs never see each other's entries.
   - The entries come from `fixtures/logs/samples/connector.jsonl` and `etl.jsonl`, with `timestamp` set to now and `insertId` and `runId` replaced as above.
6. **Denied** means `google.api_core.exceptions.PermissionDenied` (HTTP 403).
   - A positive call that hits it is retried until the deadline, because IAM grants take up to a few minutes to apply.
   - A negative call is not retried.
   - **Every negative has a positive control.** The control is the same method on the same resource, called by the identity that is allowed, and it must succeed in the same test before the negative runs. A disabled API (`SERVICE_DISABLED`) and a missing subscription also return 403, so without the control a negative proves nothing.
   - The denied identity's credentials must refresh successfully before its negative call, so "denied" means authorization failed, not impersonation.
7. **Pub/Sub messages** from the sink carry the Cloud Logging JSON `LogEntry` as `message.data`. A test pulls, acks every message it receives, and stops when it has seen one of its own insertIds.
8. **Numbers in `jsonPayload`.** Cloud Logging stores `jsonPayload` as a protobuf `Struct`, whose numbers are doubles, so an int written as `80` reads back as `80.0`.
   - T1 compares payloads with `==`, under which `80 == 80.0`.
   - `from_cloud_entry` does not coerce types back. This is an accepted limit while cloud parity is cut.
   - Anything that type-checks entries read from Cloud Logging, such as `priority-shape` fed from the cloud, would see floats. Record it in the README's limits.

## Rows

- **T1:**
  - A `CloudLoggingSink(credentials=impersonated("mock-soar"))` writes the tagged samples.
  - A `CloudLoggingQuery(credentials=impersonated("obs-reader"), extra_filter='jsonPayload.runId="<tag>"')` reads them back over `[now − 10 min, now + 1 min)` within 90 s.
  - Every entry comes back with:
    - `resource.type == "k8s_container"`;
    - all six resource labels, with `project_id` equal to the project;
    - all entry labels;
    - `severity` and `jsonPayload` equal (`==`) to what was written;
    - `insertId` unchanged.
  - `get(insertId)` returns one of them.
- **T7:** positive paths.
  - `mock-soar` writes a tagged entry.
  - `obs-reader` reads it with `CloudLoggingQuery`.
  - `obs-reader` pulls one of the run's insertIds from `fleet-sre-logs-obs` with a `pubsub_v1.SubscriberClient(credentials=impersonated("obs-reader"))` within 120 s. While waiting, `mock-soar` writes a new tagged entry every 15 s. A sink export attempted before the writer identity's publisher grant applied is dropped, not queued, so the test does not depend on the first entry.
  - On the local mock app with dev auth: `obs-reader` gets a connector instance (200), and `deployer` patches it (200).
- **T8:** negative paths. Each GCP negative asserts `PermissionDenied` with no retry, after its positive control (pin 6) succeeds:

  | Negative (denied) | Positive control (first) |
  | --- | --- |
  | `obs-reader` writes a log entry | `mock-soar` writes a log entry |
  | `mock-soar` lists log entries | `obs-reader` lists log entries |
  | `deployer` pulls from `fleet-sre-logs-obs` | `obs-reader` pulls from `fleet-sre-logs-obs` (a pull that returns no messages counts; acknowledge what it receives) |

  - On the local mock app with dev auth: `obs-reader` patching a connector instance gets 403.
  - **The SRE runtime has no credentials of its own:**
    - No file under `fleetsre/sre/` imports `google` or `fleetsre.adapters`. This is the import-linter contract, re-checked by reading the files.
    - No file in the repo is a service-account key, meaning JSON with `"type": "service_account"` and a `private_key`. The scan skips `.git/` and `.venv/`.
- **T10:**
  - It first computes the three D7 emails with `service_account_email`.
  - Then, through `google.cloud.iam_admin_v1.IAMClient` (the user's ADC), every service account in the project has no key of type `USER_MANAGED`.
  - The three D7 accounts exist.

## Unit tests (Session B, `tests/unit/`, offline)

- `to_cloud_entry` / `from_cloud_entry` round-trip every sample line, and `project_id` is replaced.
- `build_filter` and `build_get_filter` produce the exact filter strings, with RFC 3339 `Z` timestamps.
- `gcp_auth.service_account_email`.
- `infra/gcp.sh`:
  - passes `bash -n`, and `shellcheck` if it is installed;
  - `up` without `FLEETSRE_GCP_PROJECT` exits non-zero before any gcloud call (run with `PATH` stripped of gcloud).

## Pre-flight (Rixon, before the tests PR merges)

- **P1.** Application Default Credentials exist, carry a quota project, and the account can manage IAM in the project:
  ```bash
  gcloud auth application-default print-access-token >/dev/null && echo ADC ok
  gcloud auth application-default set-quota-project fleet-sre-1029-9324
  gcloud projects get-iam-policy fleet-sre-1029-9324 --flatten=bindings --filter="bindings.members:user:$(gcloud config get-value account)" --format="value(bindings.role)"
  gcloud services list --enabled --project fleet-sre-1029-9324 --format="value(config.name)" | grep -E 'logging|pubsub|iam'
  gcloud logging sinks list --project fleet-sre-1029-9324
  gcloud iam service-accounts keys list --iam-account 137122766950-compute@developer.gserviceaccount.com --managed-by=user
  ```
  - If ADC is missing, run `gcloud auth application-default login` first, then the quota-project line.
  - The default compute account must show no user-managed keys, or T10 fails on an account D7 never made.

## Session A goal

```
Objective: Write the D7 acceptance tests (deliverables.md §D7 rows T1, T7, T8, T10) exactly as
  specified in docs/d7-pins.md.
Inputs: docs/spec.md (§4.2, §7), docs/deliverables.md §1 and §D7, docs/d5-pins.md (pin 1, LogQuery),
  docs/d7-pins.md, docs/decisions/logging-resource.md, .agents/AGENTS.md.
Deliverables:
  - pyproject.toml: the four Google dependencies in d7-pins 4 (then pip install -e .), and a
    mypy override only if mypy reports missing stubs for a google.* module.
  - tests/acceptance/d7/test_d7_gcp.py: one function per ID (test_d7_t1_..., t7, t8, t10), all marked
    gcp, skipping when FLEETSRE_GCP_PROJECT is unset, and calling a D7 stub first after the skip
    check (d7-pins 5-8, Rows). T8's negatives each follow their positive control (d7-pins 6).
  - Stubs raising NotImplementedError: fleetsre/adapters/gcp_auth.py and
    fleetsre/adapters/cloud_logging.py (every name in d7-pins 1-2). The stubs import no google
    package at runtime (TYPE_CHECKING imports for annotations are fine).
Rules: no implementation logic. Never run gcloud or make any live GCP call; never set
  FLEETSRE_GCP_PROJECT to anything but the literal dummy-project, and only for the red check in
  Acceptance. Do not edit docs/, contracts/, fixtures/, prompts/, or other deliverables'
  tests.
Acceptance: pytest -m gcp tests/acceptance/d7 --co collects exactly 4 tests; with
  FLEETSRE_GCP_PROJECT unset, pytest -m gcp tests/acceptance/d7 -rs skips all 4; with
  FLEETSRE_GCP_PROJECT=dummy-project, all 4 fail with NotImplementedError; make check passes
  (gcp is deselected); make lint passes.
Stop: print a table of test ID -> function name -> what it calls first, then stop.
```

After the tests PR merges, Rixon runs, once, with `FLEETSRE_GCP_PROJECT` exported: `python -m pytest -m gcp tests/acceptance/d7 -rs`. All four must fail with `NotImplementedError`, not with an auth or import error. That is this deliverable's red. It needs no infra (pin 5).

## Settled in D7

As built in #36 (tests) and #37 (implementation).

- **Red.** The tests PR was green in CI (gcp deselected). The manual red run against the real project failed all four tests with `NotImplementedError` in 0.08 s, before any network call.
- **Pin 4.** No mypy override was needed. `make lint` type-checks `fleetsre/` and `scripts/`, not `tests/`.
- **Pin 7.** A synchronous pull on an empty subscription raises `google.api_core.exceptions.DeadlineExceeded` at the client timeout. The tests treat it as an empty pull: T7 keeps polling to its deadline, and T8's `_pull` returns normally, so `obs-reader`'s control passes and `deployer`'s negative still needs `PermissionDenied`.
- **Pin 8.** The limit is recorded on `from_cloud_entry`'s docstring. The README (D8) lists it.
- **T8 import check.** It reads every file under `fleetsre/sre/` with `ast.walk`, with no exemption for `TYPE_CHECKING` blocks.
- **Adapters.** `CloudLoggingSink` and `CloudLoggingQuery` use `LoggingServiceV2Client` directly (`write_log_entries`; `list_log_entries` with `order_by="timestamp asc"`). Severities map to Cloud Logging's numbers and back by name; an unknown severity is sent as `DEFAULT`.
- **Unit tests.** The `gcp.sh` test puts a fake `gcloud` first on `PATH` that leaves a marker if it is called, instead of stripping `gcloud` from `PATH`.

## Results (live, 2026-11-02)

- `make infra-up` created the three service accounts, the topic, the subscription, the sink and every binding in `fleet-sre-1029-9324` in one run.
- After a ~5-minute wait, `python -m pytest -m gcp tests/acceptance/d7 -rs`: 4 passed in 65 s, on the first run (`evals/d7-2026-11-02.txt`).
- The infrastructure stays up until after submission. Then: `FLEETSRE_GCP_PROJECT=fleet-sre-1029-9324 make infra-down`.
