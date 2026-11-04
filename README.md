# Fleet SRE

An agentic site reliability engineer for MSSPs running multi-tenant Google SecOps SOAR. Built for the Meta Global AI Developer Hackathon (Indie track, "Build for Your World").

## What it is

An MSSP runs many tenants (SOAR environments) across many services in one SecOps instance. Connectors fail silently: a connector can show `LIVE` while ingesting nothing, an expired credential can be swallowed into a warning, and a severity field can quietly turn from an integer into a string. Each tenant's monitoring depends on who onboarded it.

Fleet SRE does three things:

- **Authors** one monitoring contract per service from a requirements brief, revising it against a chaos scorecard that includes held-out scenarios it never sees.
- **Investigates** when a tripwire fires, using read-only tools, with every step checked by deterministic gates inside the tools.
- **Proposes** one allowlisted fix for a human to approve or reject with notes, then applies it under a separate identity and lets the tripwires decide whether it worked.

The model is not trusted. Deterministic code grades every output: contracts by the scorecard, evidence when it is recorded, diagnoses when they are submitted, and fixes by tripwires.

The SecOps it watches is a mock, built against Google's published Chronicle API reference and SOAR log format, so it could be swapped for a real instance. What the mock invents is listed under [Faithful versus invented](#faithful-versus-invented). All tenant data is synthetic.

## Run it locally

Requires Python 3.12 and `make`.

```bash
git clone https://github.com/Rixonpolvi/fleet-sre && cd fleet-sre
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make check            # lint, type check, import contracts, trace check, offline tests
make demo-check       # plays every demo act headless; exits 0 if each resolves as scripted
make demo             # serves act F1 at http://127.0.0.1:8000 and prints the admin URL
make demo ACT=F2      # or F3
```

`make demo` needs no API key. The investigator's turns come from scripted segments recorded in the same world, so the demo is deterministic and free. Open the printed admin URL to get the approve, reject, reply and "mark rotated" controls; without it the pages are read-only.

The three acts:

| Act | Fault | What happens |
| --- | --- | --- |
| F1 | Connector stall on alpha | `dead-connector` trips while the quiet tenant stays green; the SRE proposes a restart; approve; RESOLVED |
| F2 | Credential expiry on bravo (the Green Liar: `LIVE`, zero alerts) | `connector-errors` trips; the SRE compares siblings, tests the integration and escalates; the incident waits with no model calls; "mark rotated"; the tripwire recovers; the same session resumes, submits `none`; RESOLVED |
| F3 | Severity drift on alpha | `priority-shape` trips; the SRE proposes `Unknown Severity Priority = -1`; reject with "unknown severities should map to 100"; it revises in the same session; approve; RESOLVED |

The demo clock runs at 10× and holds while an incident awaits approval, so the approver's timing never changes what the investigator sees.

Other entry points:

- `make author`: one live contract-authoring run (needs `MODEL_API_KEY` and `FLEETSRE_PRICES_JSON`; about $0.50).
- `make test-live`, `make test-gcp`: the marker-gated live model and GCP tests.
- `make infra-up` / `make infra-down`: the GCP service accounts, log sink, topic and subscription (`infra/gcp.sh`, needs `FLEETSRE_GCP_PROJECT`).

## Architecture

```
Fake Sentinel ──poll──▶ Connector runtime ──▶ Ingestion (ETL)        Mock Chronicle API
                              │                     │                (read / patch / run / test)
                              ▼                     ▼                      ▲        ▲
                         Cloud Logging-shaped LogEntry stream              │        │
                                     │                                     │        │
                                     ▼                               obs-reader  deployer
                         Evaluator (tripwires, no model)                   │        │
                                     │ trip                                │        │
                                     ▼                                     │        │
                         Incident manager ──▶ SRE investigator ── tools ───┘        │
                                     │        (Meta Model API)                      │
                                     ▼                                              │
                         Human approval (web UI) ──▶ Executor (journal, apply) ─────┘
                                     │
                                     ▼
                         Verifier (change-point tripwires) ──▶ RESOLVED + postmortem
```

- **Ports and adapters.** Logs, log queries, state, clock and the Chronicle client are ports. Local adapters (JSONL, in-memory, SQLite, `SimClock`) run everything on a laptop; the Cloud Logging adapters run the same entries through GCP.
- **One contract per service.** Four tripwire types: `cadence` (a heartbeat went silent), `synthetic` (a planted canary did not arrive), `schema` (a field changed type or range) and `errorSignature` (warnings across recent runs). Tenants differ only by parameters.
- **No model in the watch loop.** The model runs when a contract is authored and when an incident opens. Watching is deterministic.
- **The SRE holds no credentials.** `fleetsre.sre` cannot import GCP clients, the adapters or the mock (import-linter contract). It reaches the world only through its tool set, whose Chronicle client is the read-only `obs-reader`. Writes go through the executor as `deployer`, only with an approval for that exact proposal hash.
- **Incident lifecycle.** OPEN → INVESTIGATING → AWAITING_APPROVAL → APPLYING → VERIFYING → RESOLVED, with NEEDS_INPUT pauses (escalation, refusal cap, budgets, stuck, model unavailable) that resume only on a human reply, granted turns, or a tripwire recovering. Rejection returns the proposal to the same session with the human's notes.
- **Executor journal.** Append-only `intent` → `started` → `step` → `terminal`. No write happens without a durable intent; `recover()` reconciles an interrupted apply so the change lands exactly once.

## Gates

The gates run inside the tools that move an investigation forward, never as tools the model may skip. A refused call comes back as an error result with problems, each prefixed by its rule code, and the model reads it and tries again. The model chooses every call; code never fixes the order.

| Rule family | Where | Refuses |
| --- | --- | --- |
| `order` | `record_evidence`, `submit_diagnosis` | Recording evidence or submitting before hypotheses are set |
| `hypotheses` | `set_hypotheses` | Fewer than 2 or more than 4 hypotheses, none marked as an alternative explanation, malformed or duplicate IDs |
| `evidence` | `record_evidence` | An `insertId` that doesn't exist, is in the future, or belongs to a deployment that is neither the incident's nor a sibling the SRE queried; a `callId` not made in this session; a `field`/`value` that doesn't match that call's result; an unknown hypothesis; a fourth item for one hypothesis |
| `diagnosis` | `submit_diagnosis` | Schema violations; evidence IDs never recorded; a concluded hypothesis without supporting evidence, or another without a refutation or open question; evidence of the wrong kind for the category (e.g. `credential_failure` needs a connector warning *and* a failed `test_integration_instance` on the tenant's own integration); a method outside the allowlist or a mask outside its fields; a resource that is not the deployment's own connector or job instance; `none` without fresh evidence after a resume |
| `reads.cap` | every read tool | Reads past the segment's budget (12, then 4 per resume) |

Around the gates: three refused submissions pause the incident (`refused`); the same read with the same arguments a third time pauses it (`stuck`); turn and token budgets pause it (`budget`, `token_budget`). The incident manager refuses an investigator whose gates are switched off; gates-off mode exists only for evaluations.

## Results

All results are committed under `evals/` and `spikes/`.

- **Gated protocol feasibility (D2).** `muse-spark-1.3` followed the full protocol (one tool call per turn, hypotheses first, evidence checked on record, gated submission) and diagnosed the credential Green Liar live (`spikes/RESULTS.md`).
- **Investigator (D5).** Six faults × 3 runs, gates on: 18 of 18 correct on the final prompt (v5), at most about $0.10 per investigation (`evals/d5-2026-11-01.json`). Gates-off runs reached the right category 6 of 6. With prompt v5 the semantic gates rarely fire: almost every refusal was schema or argument validation. The gates are a guarantee rather than a correction.
- **A gate-passing fix a human should reject.** Both recorded F3 investigations proposed `Unknown Severity Priority = -1`. The tripwire recovers and every gate accepts it, but it maps unknown severities (`Critical` included) to Informational. That is why the demo's F3 act rejects it with notes, and why approval stays human.
- **Contract authoring (D6b).** `muse-wonder-1.0`, effort `high`: **0 of 3 runs passed** (`evals/d6-2026-11-02.json`, $1.46). Every run fixed all six visible scenarios from scorecard feedback by version 3, then failed the held-out `flap-mild` scenario: to stop a flapping connector recovering mid-fault it lowered the `connector-errors` threshold until the tripwire paged on a healthy connector's 20% noise. The holdout caught exactly what it exists to catch. A sweep then showed the scorecard itself is over-constrained on one seed: 7 of about 200 `connector-errors` settings pass all eight scenarios, and the reference contract is among those that pass by luck (see [Limits](#limits)).
- **GCP and IAM (D7).** Live, 4 of 4 passed on the first run (`evals/d7-2026-11-02.txt`): `mock-soar` writes logs and cannot read them; `obs-reader` reads logs and pulls the subscription and cannot write logs or patch the mock API; `deployer` cannot pull; no service account has a key. Every negative runs after a positive control on the same API and resource, so a disabled API cannot pass as "denied".
- **Demo (D8).** `make demo-check` plays every act with the human actions at 0, 600 and 1800 s of delay; each ends RESOLVED and consumes its script exactly.

## Limits

- **The single-seed scorecard.** Each scenario is scored on one seeded stream, so a sound contract can fail and a weak one can pass on a lucky clean streak (the reference contract's `connector-errors` among them). The fix, after the hackathon: score each scenario over several seeds and recover `errorSignature` symmetrically.
- **Cloud Logging numbers come back as doubles.** `jsonPayload` is stored as a protobuf `Struct`, so an integer written as `80` reads back as `80.0`. `from_cloud_entry` does not coerce; anything type-checking entries read from the cloud (such as `priority-shape`) would see floats. Cloud parity is not built, so nothing consumes cloud entries yet.
- **Not deployed.** Pub/Sub push, Firestore, Cloud Run and Cloud Scheduler adapters are designed but cut; the mock API's permissions are enforced under dev auth locally. The web app binds to localhost only.
- **Demo.** One act at a time, each a replica of the world its scripted segments were recorded in; switching acts resets. Leaving an act's path (approving F3's `-1`, replying to F2 instead of marking it rotated) ends the script and pauses the incident as `model_unavailable`. The demo clock holds while an incident awaits approval.
- **In memory.** Incident records live in memory; only the executor journal can be a file.
- **Verifier.** It accepts any tripwire of the deployment as the verification target, including one the incident never tripped.
- **Amendments are cut.** Drafting a contract change after a late or missing trip is designed, not built.
- **`executeTest` permission.** The mock lets the read-only identity run `integrationInstances.executeTest` (it changes no state); a real deployment would need a custom role, because Google documents it under `integrationInstances.update`.

## Faithful versus invented

Section numbers refer to `docs/mocks.md`.

| Faithful (cite in code comments) | Invented (say so in the README) |
| --- | --- |
| Resource names, paths, methods and field lists (§4.1) | Fake Sentinel transport and token endpoint |
| `status` enum values, `logLevel` and `JobResultStatus` enums, `ParamType` enum | Meaning of `LIVE` (mock rule in §4.1) |
| `integrationVersion` required on connector patch; job `intervalSeconds` minimum 60 | Collection captures all log levels |
| Method request and response bodies: `executeTest`, `runOnDemand`, `setLogsCollection` | `runOnDemand` ignoring the body's values |
| ConnectorLogs collected only during a 3-hour collection period; enabling clears existing records | PASSWORD round-trip on patch |
| Permission name pattern and the method permissions in §4.2 | The two permission deviations in §4.2 |
| Log namespace, containers and entry labels (§5) | `logName`, `pod_name`, every `jsonPayload` field, event names |
| AlertInfo field names and the priority scale | The custom connector's and job's parameters |
| Sentinel incident payload fields and severities | Fault mechanics (§6) |

## Roles

The rule this project holds itself to: humans write what defines "correct", the coding agent writes what is graded against it. Every deliverable was two pull requests, a tests PR from one coding session and an implementation PR from a fresh one, with a CI guard that stops implementation branches from touching tests, contracts, fixtures, prompts or docs.

- **Muse Code** (Meta) wrote all code and tests in the repository, and generated fixtures where a deliverable allowed it. Session exports are in `provenance/`.
- **Meta models** run the system: `muse-spark-1.3` is the investigator at runtime, and `muse-wonder-1.0` authors contracts.
- **Rixon** (the human) wrote the contracts, scenarios and expected outcomes, reviewed every pull request, approved every Muse Code action outside its standing policy, and ran every live model run, `gcloud` command and live GCP test. Muse Code never ran `gcloud`.
- **Claude** (Anthropic) was used for planning and review, outside the repository's runtime:
  - drafted the per-deliverable pin files (`docs/d5-pins.md` to `docs/d8-pins.md`), the doc syncs and this README;
  - drafted `prompts/investigate.md` (v1–v5; cross-checked by Muse Code, reviewed by Rixon), `prompts/author.md` and `fixtures/briefs/alert-ingestion.yaml` (reviewed by Rixon);
  - drafted a few test and doc patches, applied by Rixon: D4 #18; D5 #23 and #24; the re-plan (#29); the D6a fixture fix; the D6b docs (#33, #35); the D7 docs sync (#38);
  - reviewed Muse Code's pull requests from their diffs.

No Claude model runs in the product.

## Built during the hackathon

Everything in this repository was built between 2026-10-28 and 2026-11-04; the first commit is on 2026-10-28. `docs/spec.md`, `docs/mocks.md` and `docs/deliverables.md` began as planning documents and were revised as each deliverable settled. Each deliverable's acceptance tests are in `tests/acceptance/dN/`.

| ID | Deliverable | What it built |
| --- | --- | --- |
| D0 | Scaffold and guardrails | `make check`, protected-path guard, trace check, secret scan and denylist hook, `.agents/AGENTS.md` |
| D1 | Contracts and scenarios | Contract schema and loader, the two service contracts, fleet, scenario library with holdouts, golden streams, mutants, Chronicle field specs |
| D2 | SRE feasibility spike | The gated loop on the Responses API, proven live on the Green Liar |
| D3 | Mock SecOps | Fake Sentinel, connector, ingestion and job runtimes, fault injection F1–F7, the mock Chronicle API with authorization |
| D4 | Evaluator and chaos harness | Pure tripwire functions, the stateful evaluator, canary scheduler, replay and scorecard |
| D5 | SRE investigator | Tool set, gates, budgets, stuck detection, transcripts, resume, recorded replay, live eval |
| D6 | Fix loop and authoring | D6a: incident lifecycle, approvals, journaled executor, verifier, postmortem. D6b: contract authoring against the scorecard with held-out scenarios |
| D7 | GCP adapters and IAM | Cloud Logging adapters, keyless identities, `infra/gcp.sh`, live air-gap tests |
| D8 | Demo and UI | Demo acts, fleet grid, incident view, turn timeline, handoff card, approve/reject/reply, `make demo` |

## Links

- Repo: https://github.com/Rixonpolvi/fleet-sre
- Video: https://youtu.be/b9Wyi15MX2M
