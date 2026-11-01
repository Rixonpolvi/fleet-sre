# D5 pins

Interfaces and semantics of the D5 investigator as built (PRs #22–#27). `deliverables.md` §D5 and `spec.md` §6 summarise them; this file has the detail and wins where they differ. `tests/acceptance/d5/` encodes the testable rules; `tests/unit/` covers the rest.

## Interfaces

1. **`fleetsre/ports.py`.**
   - `LogQuery` protocol: `entries(start, end) -> list[LogEntry]`, `get(insert_id) -> LogEntry | None`; `MemoryLogQuery(sink)`.
   - `ChronicleClient` protocol: `get(name)`, `list_connector_logs(name, page_size)`, `list_job_logs(name, page_size)`, `execute_test(name)`, `patch(name, body, update_mask)`, `run_on_demand(name, body)`, `set_logs_collection(name, enabled)`, and an `identity` attribute. Returns parsed JSON or raises `ChronicleError(status, body)` carrying the Google error body.
   - `StateStore` protocol: `record_trip(event)`, `record_change(deployment, record)`, `trip_history(deployment) -> list[dict]`; `MemoryStateStore`.
2. **`fleetsre/adapters/chronicle_http.py`.** `ChronicleHttpClient(http: httpx.Client, identity: str)` sends `X-Fleetsre-Identity` (dev auth; ID tokens come in D7).
3. **`fleetsre/registry.py`.**
   - `Registry(contracts, deployments, scorecards=None)` with `contract(service)`, `deployments(service)`, `deployment(id)`, `scorecard(service)` and `route(entry) -> list[(deployment ID, component ID)]`, using the evaluator's routing (D4 semantics item 5). D6 adds `apply_contract`.
   - `Deployment.resource_names` maps component ID → full Chronicle resource name, plus `"integrationInstance"`. `deployments_from_fleet` computes it from the fleet; `Deployment.resources` keeps its D4 meaning (routing).
4. **Import boundary.** `fleetsre.sre` may import `fleetsre.registry`, `contracts`, `logmodel`, `ports`, `clock` and `evaluator.tripwires`. It must not reach `google`, `fleetsre.adapters` or `fleetsre.mock`, transitively included. `.importlinter` sets `exclude_type_checking_imports = True`.
5. **`ToolSet(log_query, chronicle, state, registry, *, clock, verification=True)`.**
   - `call(name, args, session, *, call_id=None)` always returns `{"callId": <id>, "result": <dict>}`: exactly what reaches the model.
   - A refusal's result is `{"error": str, "problems": [str]}`. `call` never raises; an internal error is a refusal with rule `tool.error`.
   - With `call_id=None` the ToolSet assigns `local-<n>`.
   - Every call is stored in `session.calls[call_id]` as a `CallRecord(tool, args, result, verdict, problems, turn)`.
   - The gates get the incident through `Incident.reconstruct(session.incident_id, registry, now)`, which parses the ID against the registry's services, environments and tripwire IDs (never by splitting on `-`).
   - Read-tool output schemas live in `fleetsre/sre/schemas.py` as `READ_TOOL_OUTPUT_SCHEMAS`.
6. **`Incident`** has `id`, `deployment`, `tripwire`, `opened_at`, `trip_event`, `service`, `component`, `resources` and `siblings`.
   - `resources` comes from `Deployment.resource_names`; `siblings` holds deployment IDs.
   - `Incident.from_trip(event, registry, now)` sets `id = f"inc-{service}-{environment}-{tripwire}-{at:%Y%m%dT%H%M%SZ}"`.
   - `render_incident(incident, registry) -> str` builds the first user message: incident ID, deployment, `Component: <ID> (the tripped tripwire's component)`, every component of the service with its kind and full resource name, every tripwire with its component, the tripped tripwire's type and spec with `${param}` resolved (never the description text), trip reason, trip evidence insertIds, trippedAt, now, sibling IDs and budgets.
7. **`Evidence`** gains `deployment: str | None`. For a log source, `value` is `entry.model_dump(mode="json")`.
8. **`Session`** is JSON round-trippable.
   - Typed fields: `hypotheses: dict[str, Hypothesis]`, `evidence: dict[str, Evidence]`, `calls: dict[str, CallRecord]`.
   - Also: `turn`, `history` (input items), `segment_start_turn`, `turn_limit`, `read_limit`, `token_limit`, `segment_reads`, `segment_tokens`, `refused_submissions`, `resumed_at_turns`, `transcript_id` and `plan`.
   - `status` is `running | submitted | needs_input`.
   - `SessionStore`: `save`, `load`, `append_transcript(transcript_id, record)`, `transcript(transcript_id)`. Implementations: `MemorySessionStore()` (`fleetsre/sre/session.py`) and `SqliteSessionStore(path)` (`fleetsre/adapters/sqlite_state.py`).
9. **`investigate(incident, tools, gateway, store, *, max_turns=24, max_read_calls=12, max_tokens=400_000)`.**
   - With no saved session it starts one. With a saved `running` session it continues it with the budgets passed. With a saved `submitted` or `needs_input` session it raises `ValueError`.
   - `resume(incident, tools, gateway, store, *, message, extra_turns=8, extra_reads=4)` raises `ValueError` unless the saved session is `needs_input`.
   - Both return `InvestigationResult(diagnosis, status, reason, transcript_id, turns, read_calls, tokens, refusals)`. `status` is `submitted`, with `reason` the submitted action type (`propose` or `none`), or `needs_input` with a reason from pin 15. For `escalated`, `diagnosis` is set.
10. **`ModelGateway(config, transport, *, sleep=time.sleep)`.**
    - `ModelGateway.respond(role, input_items, tools)` returns `ModelResponse(output, usage)`; `tools` may be `None`.
    - `Transport.create(request: dict) -> dict` raises `TransportError(status)`.
    - `ScriptedTransport(responses)` records `.requests`; an item `{"_error": <status>}` raises `TransportError`.
    - `GatewayConfig.prices`: per model, USD per 1M tokens (`input`, `cached_input`, `output`).
    - `OpenAITransport(*, api_key, base_url)` calls the OpenAI SDK's `responses.create` with SDK `max_retries=0` and `timeout=120`. A timeout surfaces as `TransportError(504)`. The key is passed explicitly from `MODEL_API_KEY`.
    - The live role comes from `FLEETSRE_INVESTIGATE_MODEL` (default `muse-spark-1.3`), effort `medium`, prices from `FLEETSRE_PRICES_JSON`.
11. **Gate signatures.** `check_hypotheses(items)`; `check_evidence(args, session, log_query, incident, registry)`; `check_diagnosis(d, session, incident, contract, log_query, registry)`.

## Loop

12. **Input.** `[developer message: prompts/investigate.md verbatim, user message: render_incident] + history`.
    - Request flags: `store=False`, `parallel_tool_calls=False`, `include=["reasoning.encrypted_content"]`, `reasoning={"effort": role effort}`, `tools=specs()`, `model` = role model.
    - Output items are replayed as returned (`exclude_none`).
13. **Tool outputs.** Every `function_call` gets a `function_call_output` whose `output` is the string `json.dumps({"callId", "result"})`. Only the first call in a response runs. The others get the error `not run: only the first tool call in a response runs`, and don't count as calls or toward stuck. A reply with no tool call gets the user message `Plain text is not accepted. Call exactly one tool.` and counts as a turn.
14. **Last turn.** When `turn == turn_limit`, the user message `This is your last turn. Call submit_diagnosis now with what you have.` is appended before the request, after any plain-text message.
15. **Budgets are per segment.** The first investigation is segment 1, and each resume starts a new one.
    - Turns: `max_turns`, then `extra_turns`.
    - Reads: `max_read_calls`, then `extra_reads`. Only accepted calls to the eight read tools count. A read over the cap is refused (`reads.cap`) and still counts as a turn.
    - Tokens: `max_tokens`, counted from the segment start as usage input + output. The check runs after each turn; an accepted submission on that turn wins.
    - Three refused submissions in a segment end with `needs_input`/`refused`.
    - After a turn, the order is: accepted submission, then the refusal cap, then the token budget.
    - The refused-submission count and the stuck counts reset at each resume.
    - `needs_input` reasons: `escalated`, `refused`, `budget`, `token_budget`, `stuck`, `model_unavailable`.
16. **Stuck.** A read tool called with the same canonical arguments (keys sorted, `rationale` excluded) a third time within a segment ends with `needs_input`/`stuck`, before the call runs. Only executed calls count (verdict `accepted` or `wouldRefuse`). Working tools never trip it: repeating one changes session state, and three identical refused submissions reach the refusal cap instead.
17. **Backoff.** 429 and 5xx retry with delays of 1, 2, 4, 8 and 16 s (no jitter), up to `max_retries`, then `needs_input`/`model_unavailable`. Any other `TransportError` → `model_unavailable` at once. Other exceptions propagate.
18. **Transcript.** One record per turn, saved with the session after every turn. Each record holds the output items, the tool, args, verdict (`accepted | refused | wouldRefuse | no_tool_call | stuck`), problems, the output, usage (`input_tokens`, `output_tokens`, `cached_tokens`, `reasoning_tokens`) and `cost_usd`.

## Tools

19. **`query_logs(deployment, window, filter, rationale)`.**
    - `window` is `{"lastMinutes": 1..360}`, relative to `clock.now()`.
    - `filter` is `{component?, event?, minSeverity?, runId?}`, `additionalProperties: false`. A `component` that is not a component ID of the deployment's contract is refused (`tool.name`), listing the valid IDs.
    - Returns `{"entries", "total", "truncated"}`: newest first by timestamp, at most 50. Each entry is compact: `insertId`, `timestamp`, `severity`, `labels`, `jsonPayload`, `container`.
    - Querying a sibling deployment marks it queried.
20. **`get_resource(name)`** is GET only. `name` must match an `environments`, `integrationInstances`, `connectorInstances` or `jobInstances` pattern under the fixed prefix (mocks §4.1). Anything else, including `:` custom methods and `/logs/` paths, is refused (`tool.name`) with no HTTP call. Mock errors pass through as an accepted `{"error": <Google error body>}`.
21. **Log and test tools.** `list_connector_logs(name)` and `list_job_logs(name)` take full resource names and return one page of 50, newest first: `{"logs", "truncated"}`. `test_integration_instance(name)` takes integration instance names only and returns the `executeTest` body.
22. **`compare_siblings(service, component, window)`**, where `window` is `{"lastMinutes": 1..360}` as in pin 19. For each deployment of the service it returns: runs, runs at or above WARNING, ERROR runs, the last heartbeat's timestamp and insertId, alerts ingested, and the insertId of the worst recent entry. It marks every sibling queried. An unknown component is refused (`tool.name`), listing the valid IDs.
23. **`tripwire_history(deployment)`** reads the `StateStore`. **`get_contract(service)`** returns the contract JSON with every tripwire `description` removed, plus a scorecard summary only (`passed`, `expected_hit_rate`, `false_positives`, `scenarios: <count>`), with no scenario IDs.
24. **Specs.** `submit_diagnosis` carries the full §6.5 schema with its `required` lists (diagnosis, action, `expectedVerification`, `openQuestions` items) and `condition` enum `["recovers_within"]`. `record_evidence`'s `source` is `{insertId}` or `{callId, field, value}`. Every schema has `additionalProperties: false` and a required `rationale` (`maxLength` 200). `update_plan(plan, rationale)` records the plan and has no gate.

## Gates

25. **Problem strings** take the form `<rule>: <text>`. Rules:
    - Order and hypotheses: `order`, `hypotheses.count`, `hypotheses.alternative`, `hypotheses.id`.
    - Evidence: `evidence.insert_id`, `evidence.deployment`, `evidence.future`, `evidence.call_id`, `evidence.field`, `evidence.value`, `evidence.hypothesis`, `evidence.limit`.
    - Diagnosis:
      - `diagnosis.schema`, with the JSON path of the problem (`diagnosis.schema: action: 'type' is a required property`). Argument-validation errors inside `diagnosis` use this rule, not `args.schema`.
      - `diagnosis.evidence`
      - `diagnosis.coverage`
      - `diagnosis.category` (includes `unknown` without `escalate`)
      - `diagnosis.action` (method outside the allowlist; empty `escalationSummary`)
      - `diagnosis.update_mask` (mask outside the method's fields, or patch keys outside the mask)
      - `diagnosis.resource`
      - `diagnosis.incident` (`incidentId` or `deployment`)
      - `diagnosis.component`, listing the contract's component IDs
      - `diagnosis.hypothesis`
      - `diagnosis.verification` (missing, or a tripwire not of this deployment)
      - `diagnosis.none_after_resume`
    - Other: `reads.cap`, `tool.unknown`, `tool.name`, `tool.error`, `args.schema` (with the JSON path).
26. **`set_hypotheses`.** Items are `{id, statement, wouldConfirm, alternative}`, all four required. IDs match `^H[1-9][0-9]*$` and are unique. 2–4 items, at least one alternative. Replacing the set keeps evidence whose hypothesis ID remains.
27. **`record_evidence`.**
    - **Log source:** the insertId exists, its timestamp is ≤ now, and it routes (via `Registry.route`) to the incident's deployment or to a sibling queried in this session (via `query_logs` on it, or `compare_siblings`). There is no other fallback.
    - **Tool source:** `callId` is an accepted read-tool call of this session. `field` is a dotted path into the result, where numeric segments index lists and a leading `result.` is stripped. `value` must match by JSON equality, with no coercion.
    - At most 3 items per hypothesis. IDs are `E<n>`, and `recordedAtTurn` is the session turn.
28. **`submit_diagnosis`** checks, in addition to spec §6.5:
    - The schema first. A schema problem always refuses, gates on or off. With gates on and no hypotheses yet, the order problem is reported first.
    - `incidentId` and `deployment` equal the incident's.
    - `component` is a component ID of the deployment's contract.
    - `hypothesis` is a current hypothesis.
    - For `propose`: a `connectorInstances.*` method needs the deployment's connector instance name, and a `jobInstances.*` method its job instance name, by exact match. The integration instance is never a valid resource. The patch keys are within `updateMask`.
    - `expectedVerification` is required for `propose` and `none`, and optional for `escalate`.
    - For `none`: an item counts as recorded after the last resume iff `recordedAtTurn > max(resumed_at_turns)`. With no resume, every item counts.
    - Repeated submissions to the ToolSet are allowed; the loop ends at the first accepted one.
29. **Category rules** (the spec §6.5 table, tightened):
    - `connector_stalled` / `job_stalled`: the cited log evidence includes, by insertId, the deployment's latest heartbeat at or before `trip_event.at`. The heartbeat is the event of the cadence tripwire on the contract's `connectorInstance` (`jobInstance`) component, whatever the incident's component, so a canary incident can be diagnosed `connector_stalled`.
    - `credential_failure`: an entry from the deployment's connector at or above WARNING, plus a `test_integration_instance` call on the deployment's own integration instance with `successful: false`.
    - `schema_drift`: an entry that routes to the incident's deployment and for which `evaluator.tripwires.schema_violation` returns a reason. `schema_violation` applies the tripwire's `event`/`where` filters itself; `${param}` is resolved.
    - `job_hollow_success`: a `job.run.completed` of the deployment with `status: SUCCESS` and `indicatorsSynced: 0`.
    - `job_failing`: a `job.run.error` entry, or a `job.run.completed` with `status: ERROR`, of the deployment.
30. **`verification=False`.** The order, hypotheses, evidence and diagnosis rules 2–5 record `wouldRefuse`, with their problems, instead of refusing. Schema problems (`args.schema`, `diagnosis.schema`) still refuse. Budgets, the read cap and stuck still apply.
31. **The right component** for evaluation is the tripped tripwire's component.

## Recording and evaluation

32. **`make record FAULT=<F>`** runs one live investigation on the scripted fixture's world and writes `fixtures/recorded/<F>.json`, containing `{fault, seed, t0, faults, incident, responses, transcript, diagnosis}`. Every `encrypted_content` value is replaced with `"<stripped>"` at record time. Committed recordings: F1, F2, F3 (#26).
33. **Live eval (T19/T20)** writes `evals/d5-<UTC date>.json`: per run the status, reason, turns, reads, tokens, cost, refusals by rule, tools called and diagnosis; plus the F2 probe share and, per fault, one gates-off run's outcome with its would-refuse and refused counts by rule.

## Results (muse-spark-1.3, effort medium)

| Run | Prompt | Correct | Notes |
| --- | --- | --- | --- |
| 1 | v3 | 15/18 | F3 1/3 (escalated a cause a setting fixes), F5 2/3 (proposed a re-run of an upstream failure) |
| 2 | v4 | 16/18 | F1 2/3: one run hit the refusal cap on submission shape |
| 3 | v5 | 18/18 | Refusals down to a missing `alternative` field on a first `set_hypotheses` |

- At most about $0.10 per investigation: 54 investigations, plus the Muse Code sessions running at the same time, moved the credit from $144.13 to $138.58. Runs averaged about 130k input tokens, 74% cached.
- F2 probe share 1.0 in every run.
- Gates-off runs reached the right category in 6 of 6, with no would-refuse in run 3. Nearly every refusal in all runs was schema or argument validation, which refuses with gates on or off. With prompt v5 the semantic gates rarely fire; they are a guarantee, not a correction. D8 decides how to show this.
- Both F3 recordings set `Unknown Severity Priority` to `-1`, which maps unknown severities (`Critical` included) to Informational. The tripwire recovers and the gates accept it; a human approver should not.

## Notes for D6

- Done in D5: the mock's PASSWORD round-trip now uses the stored parameter's type.
- F1 trips both `dead-connector` and `canary`. A second trip on a deployment with an open incident should attach to that incident rather than open a new one.
- `query_logs` on an unknown deployment returns empty data instead of refusing.
- F3's `-1` patch is a ready-made reject-with-notes case (D6-T17): reject with "unknown severities should map to 100" and the SRE revises.
- The prompt could say every hypothesis needs `alternative` (true or false); most remaining refusals are that.
