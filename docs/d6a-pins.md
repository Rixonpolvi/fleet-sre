# D6a pins

Interfaces and semantics for D6a, the fix loop: `deliverables.md` §D6 rows T1–T10 and T16–T20. Written before the tests PR. This file wins over the §D6 interface block where they differ. D5 behaviour (`docs/d5-pins.md`) is unchanged except where pin 1 adds to it.

## Scope

- **In:** the incident lifecycle with NEEDS_INPUT pauses, approve and reject, journaled apply, verification with change points, the postmortem, and a control loop that drives it all on a `SimClock`.
- **Out:** authoring (D6b), amendment (cut), UI, GCP, and persistence across a manager restart (everything is in memory; only the executor's journal can be a file).
- **Tests:** Session A writes real tests for T1–T10 and T16–T20, and `pytest.skip("D6b")` placeholders for T11, T12, T13, T15 and T21 so the trace check passes. There is no function for the cut amendment row.
- **No new prompt.** The postmortem is built by code from the incident record. No model writes it.

## Interfaces

1. **`fleetsre/sre/investigator.py` additions.**
   - `reopen(incident, tools, gateway, store, *, message, extra_turns=8, extra_reads=4) -> InvestigationResult` continues a saved session whose status is `submitted`, and raises `ValueError` for any other status.
   - In every other respect it behaves like `resume` (d5-pins 9, 15):
     - it starts a new segment with budgets and counts reset;
     - it appends the session turn to `resumed_at_turns`, so a later `none` needs evidence recorded after it;
     - `message` is the next user input.
   - `investigate` and `resume` keep their D5 behaviour.
   - `class Investigator(tools, gateway, store, *, max_turns=24, max_read_calls=12, max_tokens=400_000, resume_turns=8, resume_reads=4)` has these members:
     - attribute `tools`;
     - `start(incident)`, which calls `investigate`;
     - `resume(incident, *, message, extra_turns=0)` and `reopen(incident, *, message, extra_turns=0)`, both passing `extra_turns=resume_turns + extra_turns` and `extra_reads=resume_reads`.
   - `ToolSet.verification` is a public read-only attribute.

2. **`fleetsre/control/approvals.py`.**
   - Records (pydantic): `Approval(id, incident_id, proposal_hash, approver, at)` and `Rejection(id, incident_id, proposal_hash, approver, notes, at)`. IDs are `A<n>` and `R<n>`.
   - `ApprovalStore(clock)` has these methods:
     - `approve(incident_id, proposal_hash, approver) -> Approval`;
     - `reject(incident_id, proposal_hash, approver, notes) -> Rejection`, where empty or whitespace-only notes raise `ValueError`;
     - `approval_for(proposal_hash) -> Approval | None`;
     - `records()`.
   - It records only and moves no incident.

3. **`fleetsre/control/proposals.py`.**
   - `Proposal(incident_id, seq, hash, method, resource, update_mask, patch, resource_version, diagnosis, created_at)`, where `diagnosis` is a dict, as in `IncidentRecord.diagnosis`.
   - `resource_version(resource: dict) -> str` returns `str(resource["version"])` for a connector instance and `resource["updateTime"]` for a job instance, chosen by the resource name (`/connectorInstances/` or `/jobInstances/`).
   - `proposal_hash(...)` is the SHA-256 hex of `json.dumps({"incidentId", "seq", "method", "resource", "updateMask", "patch", "resourceVersion"}, sort_keys=True, separators=(",", ":"))`.
   - `seq` counts from 1 per incident, so a revised proposal always has a new hash, even with an identical patch.

4. **`fleetsre/control/journal.py`.**
   - `Journal(path: str | Path | None = None)` has `append(record: dict)` and `records() -> list[dict]`.
   - With a path, each record is appended as one JSON line and flushed before `append` returns, and existing lines are loaded at construction.
   - There is no update or delete.
   - Record types (`type`): `intent`, `started`, `step`, `terminal`. Every record carries `proposal_hash` and `at` (fields in pin 18).

5. **`fleetsre/control/executor.py`.**
   - Exceptions:
     - `NotApproved`;
     - `Conflict(resource, expected, actual)`;
     - `InvalidPatch(problems: list[str])`;
     - `ApplyError(detail: str)`.
   - `ApplyResult(proposal_hash, outcome: Literal["applied", "already_applied"], api_calls: int, resource_after: dict)`.
   - `Executor(chronicle, approvals, journal, *, clock)` raises `ValueError` unless `chronicle.identity == "deployer"`.
   - Methods: `apply(incident, proposal) -> ApplyResult` and `recover() -> list[ApplyResult]`.

6. **`fleetsre/control/verify.py`.**
   - `Verifier(evaluator, registry, clock)`.
   - Methods:
     - `start(incident, diagnosis, *, changed_resource: str | None)`;
     - `check(incident_id) -> Literal["pending", "verified", "failed"]`;
     - `active() -> list[str]`.

7. **`fleetsre/control/postmortem.py`.**
   - `TimelineEvent(at, kind, detail: dict)`.
   - `Postmortem(incident_id, deployment, service, opened_at, closed_at, outcome: Literal["resolved", "escalated"], category: str | None, root_cause: str | None, action: dict | None, timeline: list[TimelineEvent], transcript_ids: list[str])`.
   - `build_postmortem(record, *, closed_at, outcome) -> Postmortem`.

8. **`fleetsre/control/incidents.py`.**
   - `IncidentState(StrEnum)`: OPEN, INVESTIGATING, NEEDS_INPUT, AWAITING_APPROVAL, APPLYING, VERIFYING, RESOLVED, ESCALATED. Each value equals its name.
   - `Pending(kind: Literal["start", "resume", "reopen"], message: str | None, extra_turns: int = 0, trigger: str)`.
   - `IncidentRecord` (pydantic) has these fields:
     - `id`, `incident` (the D5 `Incident`), `state`;
     - `tripwires: list[str]`: the opening tripwire first, then attached ones in order;
     - `reason`: the NEEDS_INPUT reason or `None`;
     - `paused_at`, `paused_snapshot: dict[str, str] | None`, `pending: Pending | None`;
     - `diagnosis: dict | None`: the latest accepted or escalated diagnosis, as `Diagnosis.model_dump(mode="json")`;
     - `proposals: list[Proposal]`, `failed_attempts: int`;
     - `timeline: list[TimelineEvent]`, `transcript_ids: list[str]`, `postmortem: Postmortem | None`.
   - `IncidentManager(evaluator, investigator, approvals, executor, verifier, state, clock, *, registry, reader)`:
     - raises `ValueError` if `investigator.tools.verification` is `False` (T20);
     - raises `ValueError` if `reader.identity != "obs-reader"`.
   - Methods:
     - `on_trip(event) -> Incident | None`
     - `tick()`
     - `approve(incident_id, proposal_hash, *, by) -> Approval`
     - `reject(incident_id, proposal_hash, *, by, notes) -> Rejection`
     - `reply(incident_id, message, *, by, extra_turns=0)`
     - `close(incident_id, *, by, note)`
     - `get(incident_id) -> IncidentRecord`
     - `incidents() -> list[IncidentRecord]`, ordered by `opened_at`, then `id`.

9. **`fleetsre/control/loop.py`.**
   - `ControlLoop(world, evaluator, scheduler, manager, sink, *, t0, tick_seconds=15)` has `step()` (one tick) and `run_until(t)`.
   - Ticks fall at `t0 + k × tick_seconds`.
   - Each tick runs the D4 replay loop's sequence (D4 semantics 1), reusing its code rather than reimplementing it:
     1. `scheduler.tick()` and `world.step_until(t)`;
     2. ingest each new sink entry in write order;
     3. `evaluator.tick()`.
   - Every event from steps 2 and 3 goes to `manager.on_trip`, in order. Then `manager.tick()` runs.
   - With the same seed, schedule and tick grid, the first trip equals the D4 harness's.

## Semantics

10. **Opening and attaching.** `on_trip(event)`:
    - Every event, trip or recovery, first goes to `state.record_trip(event)`, which is what `tripwire_history` reads.
    - **Recovery:** if the deployment has an open incident, append a `recovery` timeline event (`detail: {"tripwire"}`) to it. Return `None`.
    - **Trip on a deployment with an incident not RESOLVED or ESCALATED:** attach to it. Add the tripwire to `tripwires` if it isn't there, append an `attached` event (`detail: {"tripwire"}`), and return its `Incident`.
    - **Any other trip:** create `Incident.from_trip(event, registry, clock.now())` with state OPEN, `pending = Pending("start", trigger="opened")` and an `opened` event, and return it.

11. **`tick()`.** It works through `incidents()` in order:
    - **OPEN, or INVESTIGATING with `pending`:** set INVESTIGATING and run the pending segment synchronously to its end (no simulated time passes). Clear `pending` and handle the result (pin 12).
    - **NEEDS_INPUT:** check for a tripwire change (pin 13).
    - **AWAITING_APPROVAL:** nothing; it waits for `approve` or `reject`.
    - **APPLYING:** apply the proposal (pin 14).
    - **VERIFYING:** check verification (pin 15).
    - **RESOLVED and ESCALATED:** nothing.

    Only running a pending segment makes model calls.

12. **Handling a segment's result.**
    - Every result appends its `transcript_id` to `transcript_ids` if it isn't already there, and adds a `segment` event with status, reason and turns.
    - **`submitted`/`propose`:**
      - read the target with `reader.get(resource)`;
      - create a `Proposal` with `seq = len(proposals) + 1` and that `resource_version`;
      - move to AWAITING_APPROVAL with a `proposal` event (hash, seq).
    - **`submitted`/`none`:** call `verifier.start(..., changed_resource=None)`, move to VERIFYING, add `verification_started`.
    - **`needs_input`/any reason:**
      - move to NEEDS_INPUT with that `reason`;
      - set `paused_at = now` and `paused_snapshot = {tw: status}` for each tripwire in `tripwires`, read from `evaluator.status()`;
      - set `diagnosis` when the reason is `escalated`;
      - add a `needs_input` event.

13. **Resuming on a tripwire change.**
    - On each tick, a NEEDS_INPUT incident compares `{tw: evaluator.status()[deployment][tw] for tw in tripwires}` with `paused_snapshot`.
    - It resumes, in the same tick, only when a tripwire that was `tripped` in `paused_snapshot` is now `ok`: `Pending("resume", message=<pin 21>, trigger="tripwire")`, run as in pin 11.
    - A trip while paused (a new tripwire attached, or an `ok` one tripping) only adds an `attached` event. It does not resume, and `paused_snapshot` is not updated. The next resume message lists it under `changes` (`none -> tripped` for a newly attached tripwire).
    - Anything else means no model call. (Amended 2026-11-01 after the Session A pre-flight: F2's canary trips about 5 minutes after the pause, and resuming on it cost a model call and broke T7's one-hour quiet window.)

14. **Approve, reject, apply.**
    - **`approve(id, hash, by)`:**
      - The incident must be AWAITING_APPROVAL and `hash` its latest proposal's. Otherwise it raises `ValueError` and records nothing.
      - It records the approval in the `ApprovalStore`, moves to APPLYING and adds an `approved` event.
      - The apply happens on the next tick.
    - **`reject(id, hash, by, notes)`:**
      - It makes the same checks and records the rejection.
      - The incident moves to INVESTIGATING with `Pending("reopen", message=<pin 21>, trigger="rejection")` and a `rejected` event (notes).
      - The reopen runs on the next tick. A rejection is not a failed attempt.
    - **APPLYING, in `tick`:** `executor.apply(incident, proposal)`. The outcomes:
      - **Success:**
        - call `state.record_change(deployment, {at, method, resource, updateMask, patch, proposal_hash, approver})`;
        - call `verifier.start(..., changed_resource=resource)`;
        - move to VERIFYING with an `applied` event.
      - **`Conflict`:** pending reopen with trigger `conflict`, state INVESTIGATING, a `conflict` event. Not an attempt.
      - **`InvalidPatch`:** pending reopen with trigger `invalid_patch`. Not an attempt.
      - **`ApplyError`:** `failed_attempts += 1` and an `apply_failed` event. At 2 attempts, the incident goes ESCALATED with a postmortem; otherwise it gets a pending reopen with trigger `apply_failed`.
      - **`NotApproved`:** propagates, because it means a bug.

15. **Verification.**
    - **`start`:**
      - the target is `diagnosis.expectedVerification.tripwire`, with `started = now` and `deadline = now + seconds`;
      - with `changed_resource`, the component is the deployment's component whose `resource_names` value equals it, and the verifier calls `evaluator.enter_change_point(deployment, component, duration_s=contract.changePoint.durationSeconds, overrides=contract.changePoint.overrides)`;
      - `none` arms no change point.
    - **`check`:** returns `verified` if `evaluator.status()[deployment][target] == "ok"`; otherwise `failed` if `now >= deadline`; otherwise `pending`.
      - The check is status-based, so a tripwire that recovered before the check verifies at once (T7).
      - A target the incident never tripped also verifies at once. The gates allow any tripwire of the deployment (spec §6.5 rule 5); accepted for the MVP.
    - **In `tick`:**
      - `verified`: RESOLVED with a postmortem and a `verified` event.
      - `failed`: `failed_attempts += 1` and a `verification_failed` event. At 2 the incident goes ESCALATED with a postmortem; otherwise it gets a pending reopen with trigger `verification_failed` and moves to INVESTIGATING.

16. **`reply` and `close`.**
    - `reply` works only from NEEDS_INPUT with a message that isn't empty after stripping. Otherwise it raises `ValueError` and makes no model call.
    - It sets `Pending("resume", message=<pin 21>, extra_turns=extra_turns, trigger="reply")` and moves to INVESTIGATING. The next `tick()` runs it.
    - The segment gets `resume_turns + extra_turns` turns, so a `budget` pause resumes whether or not turns were granted.
    - `close` works only from NEEDS_INPUT, otherwise `ValueError`. It moves to ESCALATED with a `closed` event (by, note) and a postmortem.

17. **Postmortem.**
    - It is built when an incident reaches RESOLVED or ESCALATED and stored on the record.
    - `category`, `root_cause` (the diagnosis `summary`) and `action` (`type`, `method`, `resource`, `updateMask`, `patch`) come from `record.diagnosis` and are `None` when it is absent.
    - `timeline` is `record.timeline`. Event kinds:
      - `opened`, `attached`, `recovery`, `segment`, `needs_input`;
      - `resumed`, with `detail.trigger` ∈ {`reply`, `tripwire`, `rejection`, `conflict`, `invalid_patch`, `apply_failed`, `verification_failed`} and `detail.message`;
      - `proposal`, `approved`, `rejected`, `conflict`, `applied`, `apply_failed`;
      - `verification_started`, `verified`, `verification_failed`, `closed`, `escalated`.
    - A `resumed` event is added whenever a pending `resume` or `reopen` segment starts.

18. **`Executor.apply`, in order.**
    1. **Approval.** `approvals.approval_for(proposal.hash)` must exist with a matching `incident_id`; otherwise raise `NotApproved`. Nothing is journaled.
    2. **Concurrency.** Read the resource (deployer). If `resource_version(r) != proposal.resource_version`, raise `Conflict`. Nothing is journaled.
    3. **Steps.**
       - `*.patch`: the body holds the `update_mask` fields from `proposal.patch`.
         - For `parameters`, start from the resource's current list and set each proposed parameter's `value` by `displayName`, then send the full list.
         - A `displayName` not on the resource raises `InvalidPatch`, with nothing journaled.
       - `*.restart`: two steps, `{"enabled": false}` then `{"enabled": true}`, each with mask `enabled`.
       - `connectorInstances.runOnDemand`: body `{"connectorInstance": <resource read in step 2>}`.
       - `jobInstances.runOnDemand`: body `{}`.
       - Every connector patch body carries `integrationVersion` from the resource read in step 2; the mock returns 400 without it.
    4. **`intent`:** `{type, proposal_hash, approval_id, incident_id, method, resource, resource_version, steps: [{call, update_mask, body}], at}`, durable before any write.
    5. **`started`:** `{type, proposal_hash, at}`.
    6. **Each step:** the API call, then `step` `{type, proposal_hash, index, at}`.
    7. **`terminal`:** `{type, proposal_hash, outcome: "applied" | "already_applied" | "error", api_calls, detail, at}`.

    A `ChronicleError` on a step writes `terminal` with outcome `error` and raises `ApplyError`. The concurrency check runs once, before the first write; the restart macro's own version bumps are not compared.

19. **`Executor.recover()`.**
    - It handles every `intent` with no `terminal` for its `proposal_hash`, oldest first: it re-reads the resource and decides by method.
    - **Patch:** done if every masked field shows the proposed value. Parameters compare each proposed `displayName`'s value as a string; a `PASSWORD` parameter counts as done when the resource version has moved past the intent's.
    - **Restart:**
      - `enabled: false`: send only `{"enabled": true}`;
      - `enabled: true` with the version or `updateTime` unchanged since the intent: run both steps;
      - `enabled: true` with it changed: done.
    - **runOnDemand:** run it once more. This is at-least-once, because a run leaves no trace on the resource and an extra run is harmless.
    - **Done:** write `terminal` `already_applied` with `api_calls: 0`.
    - **Otherwise:** write `started`, run the remaining steps with `step` records, and write `terminal` `applied`. `integrationVersion` comes from the fresh read.
    - `recover` does not touch incidents.

20. **Identities.**
    - The ToolSet's client and the manager's `reader` use `obs-reader`; the executor's client uses `deployer`.
    - T3 makes the same patch call with an `obs-reader` client and expects 403.

21. **Messages (exact text).** Timestamps are ISO 8601 UTC with `Z`. Tests may assert these strings appear in the next model request's input.
    - Tripwire change: `Tripwire state changed since the pause at {paused_at}: {changes}. Current: {current}.`
      - `changes` is `; `-joined `{tw} {old} -> {new}`, with `old` set to `none` for a tripwire attached since the pause.
      - `current` is `; `-joined `{tw} {status}` over all of `tripwires`.
    - Reply: `Reply from {by}: {message}`
    - Rejection: `Rejected by {by}: {notes} Revise the proposal or escalate.`
    - Conflict: `Conflict: {resource} changed since the proposal (version {expected} -> {actual}). Read it again before proposing.`
    - Invalid patch: `Apply refused: {problems joined by "; "}.`
    - Apply failed: `Apply failed: {detail}.`
    - Verification failed: `Verification failed: {tripwire} on {deployment} was not ok within {seconds} s of {started_at}. Investigate again.`

## Fixtures and tests (Session A)

22. **E2E setup.** Reuse the D5 acceptance setup:
    - a seeded `World` on a `MemorySink` and `SimClock`;
    - the `Evaluator`, `CanaryScheduler` and `Registry`;
    - `ToolSet(MemoryLogQuery(sink), ChronicleHttpClient(TestClient(app), "obs-reader"), MemoryStateStore(), registry, clock=clock)`;
    - `reader` as an `obs-reader` client and the executor client as `deployer`, both on the same app;
    - a `ScriptedTransport`, whose `transport.requests` counts model calls.

    "No write call" in T7 means no patch, `runOnDemand` or `setLogsCollection` on any client. Assert it with a wrapper or by checking that `world.snapshot()` versions are unchanged.

23. **Scripted fixtures.** Incident IDs depend on the first trip.
    - For each fault, find the first trip with the implemented D4 replay loop on the D5 scripted fixture's seed, t0 and schedule, and confirm it matches that fixture's incident before reusing the fixture.
    - Where it differs, or a later segment is needed, write `fixtures/scripted/d6/<name>.json` (Session A). Never edit D5's fixtures.
    - Expected new scripts:
      - **`F2-resume`**, after the rotation:
        - one read (e.g. `test_integration_instance` returning `successful: true`);
        - one `record_evidence` after the resume;
        - `submit_diagnosis` with action `none` and `expectedVerification` on `connector-errors`.

        It may cite segment-1 evidence for the `credential_failure` rule. A hypothesis holds at most 3 items (d5-pins 27), so leave room on the concluded one.
      - **`F3-reject`:** segment 1 proposes `Unknown Severity Priority` = `-1`, as the recordings do.
      - **`F3-revise`:** after the rejection, it proposes `100`.
      - **`F1-retry`:** T5's second attempt.
    - **T5's failure path:** re-inject F1 right after each restart, so `dead-connector` doesn't recover within `seconds`. The second failure escalates.

24. **Timing.** Take clock advances from the world, never from guesses:
    - when `connector-errors` recovers after F2 is cleared (3 clean runs);
    - when `priority-shape` recovers after the F3 patch (`recoverAfter: 20`);
    - when `hollow-run` recovers after the F4 patch;
    - when `dead-connector` recovers after a restart.

    Use the D4 harness to find them. Assertions use those values plus one tick, and each test says where its numbers come from.

25. **Crash tests (T19).**
    - Simulate the crash by appending `intent`, plus `started` or `step` where needed, to a `Journal` directly.
    - For the already-applied case, apply the change to the world first.
    - Then call `recover()` on a fresh `Executor` with that journal.
    - Cover a parameter patch and a restart stopped after its first step.
    - Count patch calls with a wrapper around the deployer client.

26. **T20.** `IncidentManager(...)` with an `Investigator` whose `ToolSet(verification=False)` raises `ValueError`.
