# D8 pins

Interfaces and semantics for D8 (minimal): `deliverables.md` §D8 rows T1, T2, T3, T6, T8, T9 and T10. Written before the tests PR. **This file wins over the §D8 rows, goal and interface block where they differ**, including the text the 2026-11-01 re-plan made stale:
- there is no URL for judges;
- there is no `ChaosLoop` and no public mode;
- T8 links only the repo and the video.

North star: a working `make demo` that carries the video's F1, F2 and F3 beats with no surprises.

## Scope

- **In:**
  - a demo runtime that plays one act (F1, F2 or F3) at a time, each in the world its D6 scripted segments were generated in;
  - a local web app (admin only) with these pages: the grid, the incident view, the turn timeline and the handoff card;
  - approve, reject, reply, grant turns and close, plus one demo control: "mark rotated";
  - `make demo` and `make demo-check`;
  - the README's required structure (T8).
- **Out:**
  - public mode, the chaos loop and any deployed instance;
  - step mode;
  - persistence;
  - live streaming of turns while a segment runs;
  - intel-sync acts;
  - authoring in the UI (the authoring beat is a recorded `make author` run).
- **Who writes what:**
  - Muse writes all code and tests, and `fixtures/demo/acts.yaml` in Session A, from P1.
  - Claude drafts the README; Rixon commits it (pin 20).
- **No new model surface.** Scripted by default. With `MODEL_API_KEY` set, the investigate role is live (pin 6).

## Why acts

Each `fixtures/scripted/d6/<fault>.json` was generated in its own world: that fault's seed, t0 and schedule, with the canary scheduler on (d6a pin 23). Incident IDs depend on the first trip, and the scripts cite global `insertId`s. Running F1, F2 and F3 in one world would break all of this:
- the insertIds would shift, and `record_evidence` would refuse them;
- one `ScriptedTransport` list cannot route responses to incidents;
- F3's trip on alpha would attach to F1's open incident (d6a pin 10).

So `make demo` plays **one act at a time**, each a replica of its D6 end-to-end test world, behind one UI. Switching acts starts a fresh world.

## Interfaces

1. **`fixtures/demo/acts.yaml`** (Session A, from P1; protected once merged). One entry per act, keyed `F1`, `F2`, `F3`:
   ```yaml
   F1:
     title: "Connector stall on alpha"
     seed: <int>
     t0: "<ISO 8601 Z>"
     tick_seconds: 15
     faults:                       # exactly the D6 E2E schedule (D4 semantics 1)
       - { at: <s>, type: connector_stall, environment: alpha }
     responses:                    # scripted segment files, concatenated in this order
       - fixtures/scripted/d6/F1.json
     incident: "<expected incident ID>"
     actions:                      # what --check does; the human does these in the video
       - { on: AWAITING_APPROVAL, do: approve }
   ```
   - `do` ∈ {`approve`, `reject` (with `notes`), `mark_rotated`}. `on` is the incident state that enables the action.
   - Actions run in list order, each at most once.
   - F2: `[{on: NEEDS_INPUT, do: mark_rotated}]`.
   - F3: `[{on: AWAITING_APPROVAL, do: reject, notes: "unknown severities should map to 100"}, {on: AWAITING_APPROVAL, do: approve}]`, with responses `F3-reject` then `F3-revise`, or whatever P1 finds the D6-T17 test used.

2. **`fleetsre/demo/acts.py`.** `ActSpec` (pydantic, `extra="forbid"`) mirrors pin 1. `load_acts(path="fixtures/demo/acts.yaml") -> dict[str, ActSpec]`.

3. **`fleetsre/demo/transport.py`.** `DemoTransport(responses: list[dict])`:
   - It wraps `ScriptedTransport` and accepts `timeout`.
   - When the script is used up, it sets `exhausted = True` and raises `TransportError(400)`. Under d5-pins 17 that becomes `needs_input`/`model_unavailable` at once, with no backoff sleeps.
   - `calls: int` counts every `create`.

4. **`fleetsre/demo/runtime.py`.** `DemoRuntime(act: ActSpec, *, live: bool = False, speed: float = 10.0, lead_s: int = 60)` builds what the D6 end-to-end tests build:
   - a seeded `World` on a `MemorySink` and a `SimClock` at `t0`, with the act's faults scheduled through `world.schedule`;
   - the `Evaluator`, `CanaryScheduler` and `Registry`;
   - an obs-reader `ToolSet` (gates on), a `MemorySessionStore` and an `Investigator`;
   - an `ApprovalStore`, an `Executor` (deployer), a `Verifier` and an `IncidentManager`;
   - a `ControlLoop`;
   - the mock Chronicle app, reached in-process exactly as in d6a pin 22.

   Members:
   - Attributes `act`, `clock`, `world`, `sink`, `evaluator`, `registry`, `manager`, `store` and `loop`. `transport` is the `DemoTransport` in scripted mode, or `None` when live.
   - `lock: threading.RLock`. Every member below, and every web handler, holds it.
   - `start()` runs `loop.step()` synchronously until `first_fault_at − lead_s`, where `first_fault_at = t0 + min(fault.at)`. It records that instant as `started_at`.
   - `advance(seconds)` calls `loop.step()` until the clock has moved `seconds`, holding the clock while needed (pin 9).
   - `run_realtime(stop: threading.Event)` runs `advance(tick_seconds)` every `tick_seconds / speed` real seconds until `stop` is set.
   - `active_faults() -> list[dict]` returns the world's active faults (`id`, `type`, `environment`), by the same `World` method the `/_chaos/faults` routes use.
   - `clear_fault(fault_id)` calls `world.clear(fault_id)` and appends a `demo` timeline event `{"action": "mark_rotated", "fault": fault_id}` to the open incident on that environment, if there is one.
   - `model_calls` and `last_model_call_at` (sim time). They are counted by a wrapper on the transport, which works in live mode too.
   - `held: bool` (pin 9).

5. **`fleetsre/demo/host.py`.** `DemoHost(acts: dict[str, ActSpec], *, live=False, speed=10.0)`:
   - `current: DemoRuntime`;
   - `switch(name)` builds and `start()`s a fresh runtime, stops the realtime thread if one is running, and starts a new one only if `realtime=True` was passed to the constructor. Tests construct it without the thread.

6. **Live mode.** With `MODEL_API_KEY` set and `--live` passed, the investigate role uses `OpenAITransport` exactly as d5-pins 10 builds it, and `responses` are ignored.
   - `--check` refuses `--live` (exit 2), so CI and `make demo-check` never spend money.
   - Without `--live`, a key in the environment is ignored, so a demo never spends money unless asked. This narrows T6's "scripted model unless a key is set".

7. **`python -m fleetsre.demo`.**
   - Options: `[--act F1] [--host 127.0.0.1] [--port 8000] [--speed 10] [--live]` or `--check [--delay S ...]`.
   - **Serve:** it builds a `DemoHost`, starts the act, and serves `create_app` with uvicorn on `127.0.0.1`. It prints `Admin: http://127.0.0.1:<port>/?admin=<token>` and `Act: <name> — <title>`.
   - **`--check`:** for every act and every `--delay` (default `0 600 1800`), headless:
     - start the act;
     - `advance` one tick at a time, at most 4 simulated hours;
     - perform each action in `actions` when its `on` state is reached, plus `delay` simulated seconds for actions on `NEEDS_INPUT` (pin 9: the clock is held in AWAITING_APPROVAL, so delay does not apply there);
     - stop when the act's incident is RESOLVED or ESCALATED, or at the cap.

     It prints one line per run: `ACT {"act","delay","incident","state","modelCalls","scriptLength","exhausted","refusals","firstTripAt","resolvedAt"}`, with times as offsets from t0 in seconds. It exits 0 iff every run ends RESOLVED, with `incident` equal to the act's, `modelCalls == scriptLength` and `exhausted` false. Otherwise it exits 1.
   - **Makefile:**
     - `make demo` is `python -m fleetsre.demo --act $(ACT)`, with `ACT ?= F1`. It replaces the D0 placeholder.
     - `make demo-check` is `python -m fleetsre.demo --check`.
     - `make check` does not run `demo-check`; T6 covers it.

8. **`fleetsre/web/app.py`.**
   - `create_app(host: DemoHost, *, admin_token: str, operator: str = "operator") -> FastAPI`.
   - The token comes from `FLEETSRE_ADMIN_TOKEN`, or else `secrets.token_urlsafe(16)` at start.
   - Templates are Jinja2 under `fleetsre/web/templates/`, with inline CSS and a little vanilla JS. There is no build step, no framework and no CDN.
   - View data comes from pure functions in `fleetsre/web/views.py`: `fleet_view(rt)`, `incident_view(rt, id)` and `turn_input(rt, id, turn)`. The routes return them as JSON and render them as HTML.

## Semantics

9. **Clock hold.** While the act's incident is AWAITING_APPROVAL, `advance` does not step the loop and `held` is true; the header shows "clock held: awaiting approval".
   - Approval timing then cannot change what the scripted segments see: the reopen after a rejection and the apply after an approval run on the next tick, as in D6-T6, T8 and T17.
   - NEEDS_INPUT does not hold the clock. F2's quiet wait is real simulated time: the canary attaches, and no model call is made.
   - This is the honest framing for the video: "the demo clock waits for the approver".

10. **Routes.** An unknown incident gets 404, and a `ValueError` from the manager gets 409 `{"error": str}`.

    | Method | Path | Admin | Does |
    | --- | --- | --- | --- |
    | GET | `/` | no | Grid page (T1) |
    | GET | `/api/fleet` | no | `fleet_view` (pin 12) |
    | GET | `/incidents/{id}` | no | Incident page: view, timeline, handoff card (T2, T9, T10) |
    | GET | `/api/incidents` | no | `[{id, deployment, state, reason, opened_at}]` |
    | GET | `/api/incidents/{id}` | no | `incident_view` (pins 13–15) |
    | GET | `/api/incidents/{id}/turns/{n}/input` | no | `turn_input` (pin 14) |
    | GET | `/api/demo` | no | `{act, title, acts, now, started_at, speed, held, model_calls, last_model_call_at, active_faults, exhausted, live}` |
    | GET | `/api/version` | no | `{version}`, pin 17 |
    | POST | `/api/incidents/{id}/approve` | yes | `{proposal_hash}` → `manager.approve(..., by=operator)` |
    | POST | `/api/incidents/{id}/reject` | yes | `{proposal_hash, notes}` → `manager.reject(..., by=operator, notes=...)` |
    | POST | `/api/incidents/{id}/reply` | yes | `{message, extra_turns=0}` → `manager.reply` |
    | POST | `/api/incidents/{id}/grant` | yes | `{turns}` (1–24) → `manager.reply(id, f"Granted {turns} more turns.", by=operator, extra_turns=turns)` |
    | POST | `/api/incidents/{id}/close` | yes | `{note}` → `manager.close` |
    | POST | `/api/demo/faults/{fault_id}/clear` | yes | `rt.clear_fault` ("mark rotated"); 404 if the fault isn't active |
    | POST | `/api/demo/act` | yes | `{act}` → `host.switch` |

    Bodies are JSON, sent by `fetch`, so no form parsing is needed. Every POST returns the updated `incident_view`, or `/api/demo` for the demo routes.

11. **Admin credential.**
    - `GET` any page with `?admin=<token>`. A correct token sets the cookie `fleetsre_admin=<token>` (HttpOnly, SameSite=Strict, Path=/) and redirects to the same path without the query. A wrong one is ignored.
    - A POST is admin when the cookie or an `X-Fleetsre-Admin` header matches the token, compared with `secrets.compare_digest`. Otherwise it gets 403 `{"error": "admin credential required"}` and nothing changes.
    - Pages render action controls only for an admin request. A non-admin page is the read-only view.

12. **Grid (T1).** `fleet_view` returns:
    ```
    {"act", "now", "held", "services": [{"service", "deployments": [{"id", "environment", "health",
       "incident": {"id", "state"} | null,
       "components": [{"id", "kind", "health", "tripwires": [{"id", "status"}]}]}]}]}
    ```
    - It covers every deployment the act's registry arms, in registry order.
    - A tripwire's `status` is `evaluator.status()[deployment][tripwire]`. A component is `tripped` if any of its tripwires is, and a deployment if any component is; otherwise `ok`.
    - `incident` is the deployment's latest incident.
    - The HTML has one row per deployment and one cell per component, coloured by health, with the incident linked. The header shows the act, the sim time, `held`, the model-call count with the last call's sim time, and active faults.

13. **Incident view (T2).** `incident_view` returns:
    - `id`, `deployment`, `state`, `reason`, `tripwires`, `timeline` (`record.timeline`), and `postmortem` (`model_dump(mode="json")` or `null`).
    - `hypotheses`: from `store.load(id)`, with `id`, `statement`, `wouldConfirm` and `alternative`.
    - `evidence`: one item per recorded `Evidence`, with `id`, `hypothesis`, `stance`, `why` and `recordedAtTurn`, plus `source`:
      - a log source becomes `{"kind": "log", "insertId", "timestamp", "severity", "event", "message", "deployment"}`, resolved through the act's `LogQuery.get`;
      - a tool source becomes `{"kind": "toolResult", "callId", "tool", "field", "value"}`, with `tool` from `session.calls[callId]`.
    - `diagnosis`: `record.diagnosis` or `null`. `category` is shown prominently.
    - `proposal`: the latest proposal or `null`, with `hash`, `seq`, `method`, `resource`, `update_mask` and `diff`.
      - `diff` is a list of `{field, before, after}`. For `parameters`, there is one row per proposed `displayName`, with field `parameters[<displayName>]`. Other masked fields get one row each.
      - `before` comes from `Proposal.resource_before` (pin 18). A `PASSWORD` value shows as `********`.
      - `*.restart` and `*.runOnDemand` have an empty `diff` and a `steps` summary (`enabled: false → true`, or `run once`).
    - `verification`: `null` until a `verification_started` event. Then `{tripwire, seconds, started_at, deadline, current, outcome}`, where:
      - `current` is the target's status from `evaluator.status()`;
      - `outcome` is `pending`, or `verified` or `failed` from the latest such timeline event.

      The web layer never calls `verifier.check`. The manager owns transitions.
    - `turns`, `header` and `handoff` (pins 14–15).
    - The HTML shows all of these: hypotheses with their evidence under them, the diff as a before → after table, the verification line and the postmortem when present.

14. **Turn timeline (T9).**
    - **`turns`:** one card per transcript record (d5-pins 18), across every `transcript_id` of the record, in order:
      - `n`, the session turn;
      - `segment`: 1, plus the number of resumes before this turn;
      - `think`, the call's `rationale` (`""` if there is no tool call);
      - `act`: `{tool, args}` with `rationale` removed;
      - `observe`: the tool result, or `{error, problems}` for a refusal;
      - `verdict`, and `usage`.
    - **Colour by verdict:** `accepted` green, `refused` red, `wouldRefuse` amber, and `no_tool_call` and `stuck` grey.
    - A segment divider precedes the first card of each segment after the first, showing the `resumed` event's trigger and message.
    - **`header`:**
      - `turns` and `model_calls`: the number of records;
      - `read_calls`: accepted calls to the eight read tools;
      - `tokens`: summed `input_tokens`, `output_tokens`, `reasoning_tokens` and `cached_tokens`;
      - `cost_usd`;
      - `refusals`: refused records' problems counted by rule, the text before the first `:`.
    - **Exact input (`turn_input`).** The input of turn `n` is `[developer: prompts/investigate.md, user: render_incident(...)] + history[:i]`, where:
      - `history` is the saved `Session.history`;
      - `i` is the index of turn `n`'s first output item in `history`, found by dict equality and searching forward from turn `n−1`'s index.

      Every `encrypted_content` value is replaced with `"<encrypted: N chars>"`. Nothing else is changed, the last-turn and resume messages included. This needs no change to D5's transcript.
    - **Expansion.** A card's `<details>` loads `turn_input` on open and shows the items as indented JSON, with the developer prompt collapsed.

15. **Handoff card (T10).**
    - `handoff` is `null` unless the state is NEEDS_INPUT. Otherwise it has:
      - `reason`;
      - `hypotheses` and `evidence`, as in pin 13;
      - `last_refusal`: the last refused record's `{n, tool, problems}`, or `null`;
      - `escalation_summary`, from `record.diagnosis.action.escalationSummary` when the reason is `escalated`;
      - `paused_at`;
      - `model_calls_since_pause`: model calls made after `paused_at`, which the F2 beat shows as 0.
    - **Admin controls:**
      - reply, with a message and optional extra turns;
      - grant turns;
      - close, with a note;
      - **mark rotated**, shown only when `active_faults()` has a `credential_expiry` on the incident's environment. It posts that fault's ID to the clear route.
    - After mark rotated, the incident stays NEEDS_INPUT until the evaluator sees a tripped tripwire go `ok`. Then it resumes by d6a pin 13, which is what the video shows.

16. **Script exhaustion.**
    - If the human leaves the act's path, the next model call raises `TransportError(400)`, the incident pauses `needs_input`/`model_unavailable`, and `/api/demo` shows `exhausted: true`.
    - The header then shows "script ended: switch act to restart". Leaving the path means, for example, approving F3's `-1` proposal, or replying to F2 instead of marking it rotated.
    - Nothing crashes.

17. **Refresh.**
    - `/api/version` is the SHA-1 of the canonical JSON of: act, `held`, each incident's (id, state, timeline length, transcript length), `evaluator.status()` and `model_calls`.
    - Each page polls it every 2 s and reloads when it changes, unless a `<details>` is open. Then it shows "updated: reload" instead.
    - Sim time alone does not change the version.

18. **`Proposal.resource_before`.**
    - Session B adds `resource_before: dict` to `fleetsre/control/proposals.py`: the resource exactly as `reader.get` returned it when the proposal was created (d6a pin 12).
    - It is not part of `proposal_hash`, so the D6 tests are unaffected.

19. **Boundaries.**
    - `.importlinter` gains a contract: `fleetsre.web` and `fleetsre.demo` import no `google` package and nothing from `fleetsre.adapters.cloud_logging` or `fleetsre.adapters.gcp_auth`.
    - The `fleetsre.sre` contract is unchanged.
    - The app binds to 127.0.0.1 only.

20. **README (T8).**
    - Claude drafts `README.md` after Session B. Rixon commits it to `impl/d8` before that PR merges. Session B does not create or edit `README.md`, so T8 is red until then.
    - T8 checks structure and facts that cannot drift, not prose:
      - **Headings, in this order:** `## What it is`, `## Run it locally`, `## Architecture`, `## Gates`, `## Results`, `## Limits`, `## Faithful versus invented`, `## Roles`, `## Built during the hackathon`, `## Links`.
      - **Run it locally** contains `make demo` and `make demo-check`.
      - **Gates** names every gate rule family: `order`, `hypotheses`, `evidence`, `diagnosis`, `reads.cap`.
      - **Faithful versus invented** contains every non-empty cell of the §9 table in `docs/mocks.md`, parsed from that file at test time.
      - **Built during the hackathon** names every deliverable ID, D0 to D8.
      - **Links** has a line starting `- Repo: https://github.com/Rixonpolvi/fleet-sre` and a line starting `- Video:`. The URL is filled in on Wednesday; T8 checks only that the line exists.
      - **Limits** mentions the doubles limit (`80.0`) and the single-seed scorecard.
      - There is no link to a live instance: no `run.app` and no `Live instance`.

## Rows (tests)

All tests are offline and use `DemoHost(load_acts())` with no realtime thread, driven by `host.current.advance(...)`, and FastAPI's `TestClient(create_app(host, admin_token="t"))`. Admin requests send `X-Fleetsre-Admin: t`. The helpers go in `tests/acceptance/d8/d8_helpers.py`, imported as `from d8_helpers import ...` (d6b pin 20).

The helper `drive(rt, until_state, cap_s=14400)` advances one tick at a time until the act's incident reaches the state. The tests use it everywhere.

- **T1:**
  - After `start()`, `/api/fleet` lists every registry deployment, with every component and tripwire, and each `status` equals `evaluator.status()`.
  - The HTML at `/` contains every deployment ID.
  - Drive F1 to its first trip: alpha's connector component and its deployment are `tripped`, `incident.id` is the act's incident, and every other deployment is `ok`.
- **T2:**
  - **F3, at AWAITING_APPROVAL:**
    - `/api/incidents/{id}` has the session's hypotheses;
    - every evidence item is resolved by pin 13: a log item's `insertId` returns that entry's `timestamp` and `severity` from the sink, and a tool item's `tool` is the session's call;
    - the category is `schema_drift`;
    - `proposal.diff` is `[{field: "parameters[Unknown Severity Priority]", before: "", after: "-1"}]`, or the values P1 reports for the script;
    - `verification` is `null`.
  - The HTML contains each hypothesis statement and both diff values.
  - **Then:** reject, approve and drive to RESOLVED. `verification.outcome == "verified"`, `postmortem.outcome == "resolved"`, and the HTML contains the postmortem's category.
- **T3:**
  - **F1 at AWAITING_APPROVAL:**
    - approve with the latest hash returns 200; the state is APPLYING; `approvals.approval_for(hash)` exists with `approver == "operator"`;
    - approve with a wrong hash returns 409.
  - **Fresh F3 at AWAITING_APPROVAL:**
    - reject with notes returns 200; the state is INVESTIGATING with `pending.kind == "reopen"`; and the `ApprovalStore` holds one `Rejection` with those notes;
    - reject with empty notes returns 409.
  - **Without the header,** every POST in pin 10 returns 403, and state, approvals, `model_calls` and `active_faults()` are unchanged.
  - **The cookie:** `GET /?admin=t` sets `fleetsre_admin`. A POST with that cookie and no header succeeds. A wrong token sets no cookie.
- **T6:**
  - **(a)** `make -n demo` and `make -n demo-check` show `python -m fleetsre.demo`.
  - **(b)** `python -m fleetsre.demo --check --delay 0 600`, run as a subprocess, exits 0 and prints one `ACT` line per act and delay, each RESOLVED with `modelCalls == scriptLength`.
  - **(c)** For each act, the first trip is no later than `started_at + 300 × speed` simulated seconds, at the default speed (10). That is 5 real minutes at demo speed.
  - **(d)** `GET /` after `start()` returns 200.
  - **(e)** `python -m fleetsre.demo --check --live` exits 2.
- **T8:** as in pin 20. It reads `README.md` at the repo root; a missing file fails it.
- **T9:**
  - **F1 at AWAITING_APPROVAL:**
    - the number of `turns` equals the transcript records;
    - each card's `think` equals that call's `rationale`, and its `act.tool` and `act.args` equal the call;
    - a refused card, if any, has `problems`;
    - `header` equals the sums over the records.
  - **For every turn `n`:** `/api/incidents/{id}/turns/{n}/input` equals `transport.requests[n-1]["input"]`, with `encrypted_content` elided on both sides the same way. This is the real request, not a re-derivation.
  - **F3 after the rejection reopen:** a segment divider is present, with trigger `rejection`.
  - **The HTML:** one card element per turn, each carrying its verdict as a CSS class.
- **T10:**
  - **F2 at NEEDS_INPUT:**
    - `handoff.reason == "escalated"`;
    - `hypotheses` and `evidence` match the session;
    - `escalation_summary` is non-empty;
    - the mark-rotated control is in the admin HTML and not in the non-admin HTML.
  - **The quiet wait:** advance 3600 s. `model_calls_since_pause == 0`, and the state is still NEEDS_INPUT.
  - **Mark rotated:** POST the clear for the active `credential_expiry` fault. `active_faults()` no longer has it. Drive to RESOLVED; the diagnosis action is `none`.
  - **Fresh F2 runtimes at NEEDS_INPUT:**
    - reply `"rotated by hand"` gives INVESTIGATING, with `pending.message == "Reply from operator: rotated by hand"`, without ticking;
    - grant `{turns: 4}` gives `pending.extra_turns == 4`;
    - close `{note}` gives ESCALATED with a postmortem.

## Pre-flight (Session A, before writing `acts.yaml` and the tests)

- **P1. The act definitions.** From `tests/acceptance/d6/` and `d6_helpers.py`, report for D6-T6 (F1), T7 (F2) and T8 with T17 (F3):
  - the seed, t0 and fault schedule;
  - the services the world arms;
  - the response files and their order;
  - the expected incident ID;
  - each human action, and the sim time at which the test does it;
  - how T7 finds and clears the F2 fault;
  - the first-fault and first-trip offsets.

  Then write `acts.yaml` from it.
- **P2. The robustness check.** With a scratch script (not committed) that reuses `d6_helpers`, run each act through `ControlLoop` as pin 7's `--check` would, with the clock hold from pin 9:
  - for F1 and F3, with human actions at once;
  - for F2, with `mark_rotated` at delays of 0, 600 and 1800 s after NEEDS_INPUT.

  Report each run's final state, model calls against the script length, and any refusal. **If any run does not end RESOLVED with the script fully used, stop and report.** Do not edit fixtures.
- **P3.** Report the F3 proposal's `Unknown Severity Priority` before and after values (T2), and whether `jinja2` and `uvicorn` are already installed.

## Session A goal

```
Objective: Write the D8 acceptance tests (deliverables.md §D8 rows T1, T2, T3, T6, T8, T9, T10)
  exactly as specified in docs/d8-pins.md, which wins over the §D8 rows.
Inputs: docs/spec.md (§6, §10), docs/mocks.md (§6, §9), docs/deliverables.md §1 and §D8,
  docs/d5-pins.md, docs/d6a-pins.md, docs/d8-pins.md, .agents/AGENTS.md, tests/acceptance/d6/.
Pre-flight first: run P1, P2 and P3 (d8-pins §Pre-flight) with scratch scripts you do not commit.
  If P2 finds a run that does not end RESOLVED with the script fully used, stop and report.
Deliverables:
  - fixtures/demo/acts.yaml from P1 (d8-pins 1).
  - tests/acceptance/d8/test_d8_*.py: one function per ID (test_d8_t1_..., t2, t3, t6, t8, t9,
    t10), plus d8_helpers.py (d8-pins §Rows).
  - Stubs raising NotImplementedError: fleetsre/demo/{acts,transport,runtime,host}.py,
    fleetsre/demo/__main__.py (exits via NotImplementedError), fleetsre/web/app.py create_app
    and fleetsre/web/views.py (every name in d8-pins 2-8).
  - pyproject.toml: jinja2 and uvicorn if P3 finds them missing (then pip install -e .).
  - Makefile: demo and demo-check targets as in d8-pins 7.
Rules: no implementation logic. Do not edit docs/, contracts/, prompts/, fixtures/ other than
  fixtures/demo/acts.yaml, README.md, or other deliverables' tests. Never set MODEL_API_KEY or
  FLEETSRE_GCP_PROJECT.
Acceptance: pytest tests/acceptance/d8 collects 7 tests; every one fails with NotImplementedError
  or a missing file (README.md for T8); pytest tests/acceptance/d0 ... d6 still pass; make lint passes.
Stop: print the P1, P2 and P3 outputs, then a table of test ID -> function name -> current failure
  reason, then stop.
```

## Process notes

- **Review focus (tests):**
  - T9's input check must compare against `transport.requests`, never against `turn_input` run twice;
  - T3's 403 check must assert that nothing changed;
  - T6(b) must run the real subprocess.
- **Review focus (implementation):**
  - the clock hold (pin 9);
  - every handler taking the lock;
  - `turn_input`'s forward search;
  - that `clear_fault` uses the world's own fault list.
- **The red:** T6(b) fails on the stubbed `__main__`. No marker-gated tests are added.
- **Docs sync after merge:**
  - `deliverables.md` gets a revision line, the D8 pointer to this file, and a "Settled in D8" paragraph;
  - the D8 rows are reworded to match these pins: admin only, acts, the demo route, the README links;
  - `spec.md` §10 0:50–1:05 is set to what the act world arms (P1);
  - the handoff gets new divergence tells.

## Accepted limits (README §Limits)

- One act at a time. Each is a replica of its D6 end-to-end world, and switching acts resets.
- Scripted segments assume the act's path; leaving it ends the script (pin 16).
- The demo clock holds while awaiting approval (pin 9).
- In live mode, a running segment blocks the UI until it ends; turns appear when the segment finishes.
- Everything is in memory, and the app is localhost only.

## Settled in D8

As built in #39 (tests) and #40 (implementation).

- **Pre-flight (Session A).**
  - P1: F1 seed 102, F2 seed 103, F3 seed 104; t0 `2026-10-29T14:00:00Z`; each fault at +1830 s. The act world arms alert-ingestion on alpha, bravo, canary and charlie (registry order), and no intel-sync.
  - P2: every act ended RESOLVED with its script fully used (F1 9 calls, F2 12, F3 12), F2 at all three delays.
  - P3: F3's first proposal sets `Unknown Severity Priority` from `""` to `"-1"`. `jinja2` and `uvicorn` were added to the dependencies.
  - F3's fault keeps `params: {rate: 0.5}`; the world's default rate (1.0) would shift the insertIds.
- **Tests review (#39).** Before merge: T9 elides only the request side; the card match takes `turn-card` as a whole class token; T2 asserts the recorded `callId` and `value`; T10 asserts `rt.model_calls` is unchanged over the quiet hour. The transcript record's keys (`args`, `cost_usd`, `items`, `output`, `problems`, `tool`, `turn`, `usage`, `verdict`) and `output` as a dict were checked against a real D6 run.
- **`acts.yaml` and YAML 1.1.** A bare `on:` key parses as `True`; `load_acts` renames it back to `on`.
- **Turn input.** `turn_input` returns the runtime's request log entry for turn `n` (the scripted transport's `requests`, or the counting wrapper's in live mode), elided, and refuses any incident but `rt.act.incident`. Pin 14's rebuild from `Session.history` was not built. The log is the more literal "exact input", and each act has one incident, but D8-T9's exact-input check now compares the log with itself.
- **Diff.** Rows whose before and after are equal are omitted (pin 13 asked for one row per proposed parameter). A `PASSWORD` row masks both sides, so it never shows.
- **Scripted mode.** The investigate role's model is `demo-scripted`, priced at 0. Headers show `Model: scripted replay` or `Model: live (<model>)`, and the incident header shows `cost: n/a (scripted)` or a 4-decimal dollar cost.
- **Locking.** POST handlers are `async` and take the runtime's `threading.RLock`. Scripted segments run in milliseconds; a live segment blocks the server until it ends (§Accepted limits). `/api/demo/act` switches without holding the old runtime's lock.
- **Errors.** An unknown incident is 404 on the page, the API and turn input (`KeyError` from `manager.get`); `tests/unit/test_d8_web.py` covers it. A refused POST (409) just reloads the page.
- **Results.** `make check`: 231 passed, 1 skipped. `make demo-check`: 9 of 9 RESOLVED, 0 refusals; F1 resolved at +2040 s, F2 at +2475, +3075 and +4275 s (delays 0, 600, 1800), F3 at +2520 s.
- **Process.** Session B's rule "never run the demo with --live" also caught `--check --live`, the refusal test, which spends nothing. Later goals say "never serve the demo with --live".
