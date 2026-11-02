# D6b pins

Interfaces and semantics for D6b, authoring: `deliverables.md` §D6 rows T11, T12, T13, T15 and T21. Written before the tests PR; §Settled in D6b and §Results were added after the implementation (#33, #34) and the live eval. This file wins over the §D6 interface block where they differ. D5 and D6a behaviour is unchanged, except that pin 4 adds to the gateway.

## Scope

- **In:**
  - the authoring loop: brief → contract → scorecard → feedback → revision;
  - JSON output through `text.format`;
  - the alert-ingestion brief;
  - the live authoring eval;
  - a CLI for the demo beat (0:20–0:50).
- **Out:**
  - amendment and `Registry.apply_contract` (cut);
  - loading an authored contract into the registry. A human reads the CLI or eval output, and nothing writes to `contracts/`;
  - an authoring UI;
  - intel-sync authoring evals.
- **Tests:** Session A replaces the five `pytest.skip("D6b")` placeholders in place. T15 is `live`.
- **Human-owned files in the tests PR** (drafted by Claude, reviewed by Rixon):
  - `prompts/author.md`;
  - `fixtures/briefs/alert-ingestion.yaml`;
  - this file;
  - the docs edits in §Docs.

## Framing

The scorecard matches events on (deployment, tripwire ID) (D4 semantics 3), so an authored contract must use the tripwire IDs the scenarios name. The brief therefore gives:
- the required checks, by ID, with the component each watches and what it must catch;
- the components and their log routing;
- the deployment parameters;
- the event names.

The model chooses each tripwire's type and spec: fields, enums, thresholds, windows, deadlines, `consecutive` and `recoverAfter`, and the change point. That is the authored part, and the part the scorecard grades. The README and the video say "authors the tripwire specs from a requirements brief", not "designs the monitoring from scratch".

## Interfaces

1. **`fleetsre/authoring/brief.py`.**
   - `ServiceBrief` (pydantic, `extra="forbid"`) has these fields:
     - `service`, `version`, `summary: str`;
     - `components: list[BriefComponent]`;
     - `parameters: dict[str, BriefParameter]`;
     - `events: list[BriefEvent]`;
     - `checks: list[BriefCheck]`;
     - `notes: list[str]`;
     - `sample_files: list[str]`.
   - `BriefComponent(id, kind, description, logs: dict)`, where `logs` has the contract's `logs` shape (`container_name`, `match`, `deploymentBy`).
   - `BriefParameter(type, description)`, `BriefEvent(event, container, description)` and `BriefCheck(id, component, detects)`.
   - `load_brief(path) -> ServiceBrief` reads YAML.
   - `sample_files` are repo-relative JSONL paths; the caller reads them with `read_jsonl`.

2. **`fleetsre/contracts.py`.** `parse_contract(data: dict) -> Contract` runs exactly `load_contract`'s checks, in the same order and with the same codes, on a dict. `load_contract(path)` becomes read plus `parse_contract`, so D1-T1 and D1-T4 keep passing unchanged.

3. **`fleetsre/authoring/schema.py`.**
   - `flatten_schema(schema: dict) -> dict`:
     - inlines every local `$ref` (`#/$defs/…`, `#/definitions/…`);
     - drops `$defs`, `definitions`, `$schema`, `$id` and `$comment`;
     - raises `ValueError` on a recursive `$ref` (the API returns 400 on one; spec §8).
   - `authoring_schema() -> dict` is `flatten_schema(contracts/schema.json)` with these removed:
     - `provenance`, from the top-level `properties` and `required`;
     - the top-level `patternProperties` (the `_`-key allowance).

     The result contains no `$ref`.

4. **`fleetsre/sre/gateway.py` additions.**
   - `ModelGateway.respond(role, input_items, tools, *, text_format: dict | None = None)`.
     - With `text_format`, the request carries `text={"format": text_format}`, with no `tools` and no `parallel_tool_calls`.
     - Every other field is as in d5-pins 12: `store=False`, `include=["reasoning.encrypted_content"]`, `reasoning={"effort": …}`, `model`.
     - Passing both `tools` and `text_format` raises `ValueError`.
   - `RoleConfig` gains `timeout_s: float = 120` and `retry_timeouts: bool = True`.
   - `Transport.create(request, *, timeout: float)`. The gateway passes the role's `timeout_s`:
     - `OpenAITransport` passes it to `responses.create`;
     - `ScriptedTransport` records it in `.timeouts`, alongside `.requests`.
   - `TransportError(status, *, timeout=False)`. `OpenAITransport` sets `timeout=True` on a client-side timeout, still with status 504.
   - With `retry_timeouts=False`, an error with `timeout=True` is not retried. 429 and other 5xx still are (d5-pins 17). This keeps a 600 s high-effort call from being paid for up to six times.
   - **Live config.** The builder that reads `FLEETSRE_INVESTIGATE_MODEL` also adds `roles["author"] = RoleConfig(model=$FLEETSRE_AUTHOR_MODEL or "muse-wonder-1.0", reasoning_effort=$FLEETSRE_AUTHOR_EFFORT or "high", timeout_s=300, retry_timeouts=False)`.
     - P1's realistic version-1 call took 48 s at `high`, so the effort is `high`. A 300 s timeout is about 6× that, and caps the wait on a hung call like P1's `strict: false` one.
   - **Prices** come from `FLEETSRE_PRICES_JSON`, which must include `"muse-wonder-1.0": {"input": 4.00, "cached_input": 0.20, "output": 20.00}`. The CLI and T15 raise before the first call if the author model has no price.

5. **`fleetsre/authoring/loop.py`.**
   - `AuthoringVersion` (dataclass):
     - `index: int`, counting from 1;
     - `status: Literal["invalid_json", "invalid_contract", "failed", "passed"]`;
     - `problems: list[str]`;
     - `contract: dict | None`: the parsed JSON after pin 9's overwrites, or `None` for `invalid_json`;
     - `scorecard: Scorecard | None`: set for scored versions only;
     - `feedback: str | None`: the message sent after this version, or `None` for the last one;
     - `usage: dict` and `cost_usd: float`.
   - `AuthoringResult` (dataclass):
     - `passed`;
     - `stop_reason: Literal["passed", "max_revisions", "model_unavailable"]`;
     - `contract: Contract | None`: the passing contract; otherwise the last version that parsed; otherwise `None`;
     - `versions: list[AuthoringVersion]`;
     - `scorecards: list[Scorecard]`: one per scored version, in order;
     - `model: str`;
     - `tokens: dict`: summed `input_tokens`, `output_tokens`, `cached_tokens` and `reasoning_tokens`;
     - `cost_usd: float`.
   - `author_contract(brief, samples, scenarios, gateway, *, max_revisions=3, scorer=scorecard) -> AuthoringResult`.
     - `scorer(contract, scenarios) -> Scorecard` defaults to D4's `scorecard`.
     - Tests pass the real scorer or a caching wrapper around it, keyed by the contract's canonical JSON. Never a fabricated `Scorecard`.
   - `render_brief(brief, samples, schema, *, versions) -> str` (pin 8) and `render_feedback(version, scenarios) -> str` (pin 10) are public, so tests can call them.

## Semantics

6. **Versions.**
   - Version 1 is the first model call, and each revision is one more, so there are at most `max_revisions + 1` calls (4 by default).
   - The loop stops:
     - at the first passing version (`passed`);
     - after version `max_revisions + 1` (`max_revisions`);
     - when the gateway gives up (`model_unavailable`, pin 12).

7. **Model input.**
   - The input is `[developer: prompts/author.md verbatim, user: render_brief(...)]`. Then, for each earlier version: its output items as returned (`exclude_none`, as in d5-pins 12), followed by a user message holding its feedback (pin 10).
   - Role `author`.
   - `text_format = {"type": "json_schema", "name": "contract", "schema": authoring_schema()}`, with no `strict` key (P1).
     - `strict: true` returns 400 (`minProperties` is not supported for structured outputs). `contracts/schema.json` is never changed to suit strict mode.
     - With `strict` omitted, the output is not guaranteed to satisfy the schema (P1's output broke `uniqueItems`), so `parse_contract` stays the real check (pin 9).

8. **`render_brief`** builds the first user message from exactly these sections, in this order, joined by blank lines:
   ```
   # Service brief
   <yaml.safe_dump(brief.model_dump(exclude={"sample_files"}), sort_keys=False)>
   # Sample log entries
   <one LogEntry per line: json.dumps(entry.model_dump(mode="json"), separators=(",", ":"))>
   # Contract JSON schema
   <json.dumps(schema, indent=2)>
   # Versions
   You have {versions} versions. After each failed version you get feedback; reply with the whole revised contract.
   ```
   It contains nothing from any scenario.

9. **Reading a version.**
   1. **Text.** Concatenate the `output_text` parts of `message` output items, ignoring reasoning items. Strip whitespace and one surrounding code fence (with or without `json`).
   2. **Parse.** `json.loads` it. A failure gives `invalid_json` with problem `json: <error>`; a value that isn't an object gives `json: not an object`.
   3. **Overwrite.** Set `service` and `version` from the brief, and set `provenance` to `{"authoredBy": <role model>, "scorecard": "author-v<n>", "approvedBy": null}`. The schema allows `string | null` for `scorecard` and `approvedBy` and requires only `authoredBy`; `authoring_schema()` removes `provenance`, and with the top-level `additionalProperties: false` the model cannot output it.
   4. **Validate.** Call `parse_contract`. A `ContractError` gives `invalid_contract` with problem `contract.<code>: <message>`, and the scorer is not called.
   5. **Score.** Call `scorer(contract, scenarios)`. The version is `passed` if and only if `Scorecard.passed` (every scenario, holdouts included); otherwise it is `failed`.

10. **Feedback (exact text).** `{n}` is the version number. A scenario is visible when its `holdout` is false. The lines are joined by `\n`.
    - **`invalid_json`:** `Version {n} is not valid JSON: {error}. Return the whole contract as one JSON object.` Here `{error}` is `str()` of the `json.JSONDecodeError` from step 2, and the problem is `json: {error}`.
    - **`invalid_contract`:** `Version {n} was rejected by the contract loader: {code}: {message}`. Here `{code}` is `ContractError.code` and `{message}` is `str()` of the error; the problem is `contract.{code}: {message}`.
    - **`failed`, with at least one visible scenario failing:**
      ```
      Version {n} failed the scorecard. {p} of {v} visible scenarios passed.
      {scenario_id}: missing {items}; unexpected {items}
      ```
      - There is one `{scenario_id}` line per failing visible scenario, in input order.
      - Nothing is said about holdouts, whether or not one also failed.
      - `{p}` and `{v}` count visible scenarios only.
      - An empty item list renders as `none`. Items are joined by `, `, in the scorecard's order.
      - Each list shows at most its first 10 items; if there are more, it ends with `, and {k} more`. (P1's version 1 had 120 unexpected events in one scenario, a schema tripwire flapping on every alert; the first 10 show the pattern.)
      - A missing trip renders as `trip {tripwire} on {deployment} within {withinSeconds} s of the fault`. A missing recovery renders as `recovery {tripwire} on {deployment} within {withinSeconds} s of the fault clearing`.
      - An unexpected item renders as `{kind} {tripwire} on {deployment} at +{offset} s`, where `offset` is whole seconds from the scenario's t0.
      - A late event therefore appears in both lists (D4 semantics 3).
    - **`failed`, only held-out scenarios failing:** exactly `Version {n} failed the scorecard: a held-out scenario failed.`
    - Holdouts are mentioned only when every visible scenario passed, and then only in that sentence: no ID, fault, count or result.
    - If `ScenarioResult.missing` items don't carry `withinSeconds` (P2), the code reads it from that scenario's `expected`. Only the rendered line reaches the prompt.

11. **What the model never sees:**
    - any scenario's `expected` block, `mustNotTrip`, fault schedule, seed or duration;
    - a held-out scenario's ID or result.

    Visible failing scenario IDs do appear (pin 10).

12. **Model errors.** When the gateway gives up after its retries (the error D5 maps to `model_unavailable`), the loop stops with `stop_reason="model_unavailable"` and keeps the versions it has. Other exceptions propagate.

13. **Cost.** Each version's usage and cost come from the gateway (d5-pins 10). `AuthoringResult.tokens` and `cost_usd` are the sums.

## CLI and eval

14. **CLI.**
    - Command: `python -m fleetsre.authoring --brief <path> [--runs N] [--max-revisions 3] [--script <file>] [--out <path>]`.
    - `make author` runs it with `--brief fixtures/briefs/alert-ingestion.yaml --runs 1`.
    - Scenarios: every `fixtures/scenarios/<brief.service>/*.yaml`.
    - Without `--script` it needs `MODEL_API_KEY` and a price for the author model (pin 4). With `--script` it runs a `ScriptedTransport` from that file; `fixtures/scripted/d6b/demo.json` is the offline demo.
    - Stdout:
      - one line per version: `VERSION {"run","version","status","visiblePassed","visibleTotal","holdoutsPassed","falsePositives","problems","costUsd"}`;
      - one line per run: `RESULT {"run","passed","stopReason","versions","costUsd"}`.
    - Output file (default `evals/authoring/<UTC timestamp>.json`), keys as T15 asserts them:
      ```
      {"runs": [{"run", "passed", "stop_reason", "contract", "tokens", "cost_usd",
                 "versions": [{"version", "status", "problems", "feedback", "usage", "cost_usd",
                               "scenarios": [{"scenario_id", "holdout", "passed", "missing", "unexpected"}]}]}]}
      ```
      - `scenarios` lists every scenario, holdouts included, with `holdout` marking them. The file never reaches the model. It is empty for an unscored version.
      - `missing` and `unexpected` are rendered as in pin 10, uncapped.
      - `contract` is the final contract's JSON, or `null`.
    - The CLI exits 0 when every run completes, whatever their outcomes, and non-zero only on an error (no key, no price, a crash).
    - No output file contains `encrypted_content` or request bodies.

15. **T15.** It runs the CLI's code path with three runs against alert-ingestion and writes `evals/d6-<UTC date>.json` in the pin 14 format. It passes when at least 2 of the 3 runs pass. Expect about $0.15–0.25 per call (P1: $0.13 for version 1, with later versions carrying the history), so at most about $1 per run and $3 per eval.

## Time-box and demo

- **The live authoring eval is time-boxed** to two eval rounds, with at most one revision of `prompts/author.md` between them. That is about $6 and two hours. (One round was run; §Results says why the second was not.)
- If T15 still misses, the impl PR merges with the offline tests green. The live result is committed in `evals/` and reported as it is, in the README and in the video. A run that passes every visible scenario and then fails a holdout shows the holdouts working.
- "Never cut the authoring loop" (spec §9) covers the loop and its offline tests, not the live pass rate.
- **The video's authoring beat (0:20–0:50)** uses a recorded live run where version 1 really failed and a later version passed. It is introduced as one of the three eval runs, next to the T15 pass rate.
  - Use `fixtures/scripted/d6b/demo.json` only for `make demo` and the tests. If it ever appears in the video, it is labelled as scripted.
  - If no live run revises and passes, the beat shows the real runs as they are.
- **Cut order if Monday slips:** D6b first, then D8, then D7. D7 feeds only the IAM part of the 2:30–2:50 beat; D3-T27 and D6-T3 already show the mock API's 403s under dev auth.

## Settled in D6b

Decided in the implementation (#34) and accepted in review:

- **Scripted runs.** With `--script`, each run gets a fresh `ScriptedTransport` loaded from the file, so `--runs N` replays the same sequence N times. No key is needed, and cost is 0 unless the author model has a price.
- **Progress and durability.** `author_contract(..., on_version=None)` calls `on_version` once per version as soon as it is final (after its feedback is set, or as the last version). The CLI uses it to print each `VERSION` line with `flush=True` as the version finishes. The eval file is rewritten after every run, through a temp file in the same directory and `os.replace`, so a crash keeps the finished runs.
- **CLI exit codes.** 0 when every run completes, whatever the outcomes; 2 for a configuration error (no key, no price for the author model, no scenarios, a bad script file). An exception propagates.
- **Transports without `timeout`.** The gateway passes `timeout` only if `transport.create` accepts it (checked by signature), so test doubles written against the D5 interface keep working. Every transport in `fleetsre/` and `scripts/` accepts it. Accepted for the MVP; the cleaner fix is updating those doubles.
- **`parse_contract`** holds every loader check; `load_contract` keeps its `schema_invalid` code for a file that is not JSON.

## Results (live, 2026-11-02)

**T15 failed: 0 of 3 runs passed.** `muse-wonder-1.0`, effort `high`, `prompts/author.md` v1, 4 versions per run. $1.46 for all three runs (about $0.06–0.23 per call). File: `evals/d6-2026-11-02.json`.

| Run | v1 | v2 | v3 | v4 | Final `connector-errors` |
| --- | --- | --- | --- | --- | --- |
| 1 | 2 visible fail + holdout | 1 visible + holdout | holdout only | 1 visible + holdout | 5 of 11 |
| 2 | 2 visible fail + holdout | 3 failing | holdout only | holdout only | 6 of 14 |
| 3 | 2 visible fail + holdout | 3 failing | holdout only | holdout only | 9 of 20 |

- **Version 1 failed the same way in every run:** `severity-drift` (`priority-shape` flapping with a low `recoverAfter`, 110–130 events) and `flap-severe` (`connector-errors` recovering and re-tripping), plus the `flap-mild` holdout. The feedback fixed both visible failures: every run passed all six visible scenarios by version 3.
- **Every run then stalled on `flap-mild`.** To stop `flap-severe` recovering mid-fault, the model asked for more clean runs before recovery by lowering `threshold` relative to `windowRuns`, so the tripwire fired on 20% noise. "A held-out scenario failed" gave it nothing further.
- **The scorecard is over-constrained on one seed.** A sweep of `connector-errors` (`threshold` from half to all of `windowRuns`, windows 5–20, everything else the reference contract) found 7 settings of about 200 that pass all eight scenarios: 8/10, 9/11, 9/12, 10/12, 10/13, 10/16, 10/17.
  - `flap-severe`'s seeded stream has a run of 6 clean runs ending near +2940 s, at a 70% fault rate. A contract that trips before it and needs 6 or fewer clean runs to recover fails.
  - 8/10 (the reference contract), 9/11, 9/12, 10/12 and 10/13 pass because they trip after the streak: luck, as D4 semantics 12 warned.
  - 10/16 and 10/17 pass on the merits, at the edge of three constraints: at least 7 clean runs to recover, no trip on 20% noise, and a trip within `green-liar`'s 600 s (10 failing runs).
- **Round 2 was not run.** A prompt change could only steer the model toward this seed's narrow band, not toward better judgement. The time-box's fallback applies: the loop merged with offline tests green, and the result is reported as it is.
- **After the hackathon:** score each scenario over several seeds, and adopt D4's deferred symmetric recovery rule for `errorSignature`. Both remove the luck.
- **Demo (0:20–0:50):** a recorded live run where version 1 fails three scenarios, the model revises until every visible scenario passes, and the held-out scenario catches that the contract would page on a healthy connector's noise, so a human does not approve it.

## Tests (Session A)

16. **Setup.**
    - The brief comes from `load_brief("fixtures/briefs/alert-ingestion.yaml")`, and the samples from its `sample_files`.
    - The scenarios are every `fixtures/scenarios/alert-ingestion/*.yaml`.
    - Use `ScriptedTransport` with the role `author` configured, plus a module-scoped caching wrapper around the real `scorecard` (pin 5).
    - Build response sequences from the single-response files in pin 18.

17. **Rows.**
    - **T11:**
      - `[fail-visible, pass]` gives `passed`, `stop_reason="passed"`, 2 versions and 2 scorecards.
      - Request 2's last input item is a user message equal to `render_feedback` of version 1. Independently, it contains each failing visible scenario's ID and each rendered item (the first 10 per list, then the `and {k} more` tail), built by the test from the pin 10 formats and version 1's scorecard.
      - Request 1 has `text.format.type == "json_schema"`, `text.format.schema == authoring_schema()`, no `tools` and no `parallel_tool_calls`, plus timeout 600 in `.timeouts`.
      - Version 1's output items are replayed in request 2.
      - `[fail-visible] × 4` with `max_revisions=3` gives `passed=False`, `stop_reason="max_revisions"`, 4 versions and 4 requests. With `max_revisions=1` there are 2 requests.
    - **T12:** Run over every request from T11's first case and from T21's sequences, checking developer and user items only (assistant items are the model's own output). None contains:
      - `"expected":`, `\nexpected:`, `mustNotTrip`, `withinSeconds`, `"faults"` or `faults:`;
      - `json.dumps` of any scenario's `expected` (from `model_dump(mode="json")`);
      - any fault `type` string from any scenario.
    - **T13:**
      - `[invalid-json, invalid-contract, pass]` gives statuses `invalid_json`, `invalid_contract`, `passed`, and one scorecard.
      - A spy on the scorer shows exactly one call, with the `Contract` from `parse_contract`.
      - The two feedback messages equal the pin 10 texts.
    - **T15 (live):** as in pin 15. It is skipped without `MODEL_API_KEY`, like the D5 live tests.
    - **T21:**
      - Rename every holdout to a distinctive ID (`hz-<orig>-q7`) with `model_copy`.
      - `[fail-holdout, pass]` gives `passed`. Version 1's feedback is exactly `Version 1 failed the scorecard: a held-out scenario failed.`, and version 1's scorecard shows every visible scenario passed and at least one holdout failed.
      - `[fail-holdout] × 4` gives `passed=False` with every visible scenario passing, so `passed` requires the holdouts.
      - No request's developer or user items contain a renamed holdout ID.

18. **Fixtures: `fixtures/scripted/d6b/` (Session A).**
    - Each file holds one Responses API response: `{"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "<contract JSON>"}]}], "usage": {...}}`, with D5-shaped usage. `demo.json` instead holds a list (fail-visible, then pass).
    - **`fail-visible.json`:** one `fixtures/mutants/` contract, without its `_mutation` key, that fails at least one visible scenario.
    - **`pass.json`:** `contracts/alert-ingestion.json` as is.
    - **`fail-holdout.json`:** the reference contract with one spec value changed so that every visible scenario passes and at least one holdout fails. Start with a lower `connector-errors` `threshold`, so that `flap-mild` trips.
    - **`invalid-json.json`:** the reference contract's text cut off mid-object.
    - **`invalid-contract.json`:** a contract that fails `parse_contract` with `duplicate_tripwire` after pin 9's overwrites.
    - Pre-flight every contract file with the real scorer (P4) and paste the outcomes into the PR description.

19. **Session B unit tests** (`tests/unit/`), for behaviour no acceptance row reaches:
    - the 10-item cap and its `, and {k} more` tail (pin 10);
    - `flatten_schema` raising on a recursive `$ref` and inlining `#/definitions/…` (pin 3);
    - `retry_timeouts=False`: one call on a timeout, while 429 still retries (pin 4);
    - `model_unavailable` keeping the versions already made (pin 12);
    - code-fence stripping and the `json: not an object` problem (pin 9);
    - per-version cost and the summed tokens (pin 13).

20. **Conftest collision.**
    - Move the d6 helpers into `tests/acceptance/d6/d6_helpers.py`, imported as `from d6_helpers import ...`. The D6a test files change only their import lines.
    - `python -m pytest tests/acceptance/d5 tests/acceptance/d6 --co -q` must collect.

## Pre-flight (before the tests PR merges)

- **P1 results so far (2026-11-01, `muse-wonder-1.0`, toy contract, effort `low`).**
  - `strict` omitted: 200 in 6.2 s. Output items `reasoning` then `message`. The output parsed but broke `uniqueItems`, so the schema is not enforced strictly.
  - `strict: false`: the client timed out after 600 s. One sample can't say whether `strict: false` causes the hang, but omitting the key works, so pin 7 omits it.
  - `strict: true`: 400 at once, `JSON schema keyword \`minProperties\` is not supported for structured outputs`.
  - The usage fields match Spark's: `input_tokens_details.cached_tokens` and `output_tokens_details.reasoning_tokens`, plus a `cache_write_tokens` field that came back null.
  - The 600 s hang is why pin 4 does not retry timeouts.
- **P1 realistic version 1 (2026-11-01, effort `high`, `strict` omitted, `author.md` + brief + 14 samples + schema).**
  - 48.3 s; 6,977 input tokens (none cached), 5,268 output (4,660 reasoning); $0.133.
  - The output parsed with no schema errors and loaded. It used the brief's IDs, components and routing.
  - Scorecard: `expected_hit_rate` 1.0, 123 false positives, `passed=False`.
    - **Passed:** `clean`, `green-liar`, `quiet-tenant`, `stall` (visible) and `recovery` (holdout).
    - **`severity-drift` (visible):** `priority-shape` with `recoverAfter: 1` flapped trip/recovery on almost every alert (120 events).
    - **`flap-severe` (visible):** `connector-errors` at 3 of 5 tripped as expected, then recovered after 3 clean runs and tripped again.
    - **`flap-mild` (holdout):** 3 of 5 trips on 20% noise.
  - The feedback points straight at both visible fixes (a higher `recoverAfter`; an error signature needing more clean runs to recover), and the second fix likely clears the holdout too. This is the revision beat the demo needs.
  - `flap-severe` is the scenario D4 flagged as passing on seed luck (D4 semantics 12). A contract that is right in spirit can fail it on a lucky clean streak. If T15 misses only on it, that is why.
  - `ScenarioResult.unexpected` holds `TripEvent`s (`kind`, `deployment`, `tripwire`, `at`, `reason`, `evidence`). The offset in pin 10 is `at` minus the scenario's t0. The type of `missing` items is still P2.
- **P1 (Rixon, live, about $0.10).** Run `probe_text_format.py` with `muse-wonder-1.0` and the flattened schema. Record:
  - whether `text.format` returns 200 with `strict` omitted, `false` and `true`;
  - whether the output parses and validates;
  - the latency at `low` and `high` effort;
  - the usage fields;
  - whether reasoning items come back.

  This settles pin 7's `strict` and pin 4's effort.
- **P2 (settled by Session A).** `ScenarioResult.missing` items are `(kind, deployment, tripwire)` tuples with no deadline; `withinSeconds` comes from the scenario's `expected.trips` (a missing trip) or `expected.recoveries` (a missing recovery), matched by environment and tripwire. `unexpected` items are `TripEvent`s. A full alert-ingestion scorecard takes 0.3 s.
- **P4 (settled by Session A).** The reference contract passes all 8 scenarios.
  - `fail-visible` is the `lazy-canary` mutant (canary `deadlineSeconds` 1800). It fails `green-liar` and `stall` (visible) and `recovery` (holdout).
  - `fail-holdout` is the reference with `connector-errors` `threshold` 8 → 3. It passes every visible scenario and fails only `flap-mild`. Thresholds 2–6 trip `flap-mild`; 7 passes it.
  - `invalid-contract` raises `duplicate_tripwire` (a second `canary`). `invalid-json` is cut inside the first component.
- **P5 (settled by Session A).** `render_brief` is 25,226 characters: brief 3,835, samples 9,144, schema 12,067.
- **P3 (settled 2026-11-01).**
  - `provenance` requires only `authoredBy` (a string); `scorecard` and `approvedBy` are `string | null`. Pin 9 uses `null` for `approvedBy`.
  - The flattened `authoring_schema()` is 5,483 characters with no `$ref`. Its keywords are `additionalProperties` (including as a schema, for `parameters`), `const`, `default`, `description`, `enum`, `items`, `minItems`, `minProperties`, `minimum`, `oneOf`, `pattern`, `properties`, `propertyNames`, `required`, `title`, `type` and `uniqueItems`.
  - `oneOf`, `propertyNames`, `minProperties`, `uniqueItems` and a map-shaped `additionalProperties` are what strict modes usually reject, so `strict: true` is expected to fail (pin 7).
- **P4 (Session A).** Get the real scorecard outcome for each contract file in pin 18. If no single-value change gives fail-holdout, stop and report.
- **P5 (Session A).** Report the character count of `render_brief` with the real brief, samples and schema, to refine the cost estimate.

## Docs (in the tests PR)

- **`deliverables.md`:**
  - Under the revision lines, add `Revised 2026-11-01 (night) before D6b: D6b interfaces and semantics are in \`docs/d6b-pins.md\`; D6a recorded as built.`
  - In the §D6 re-plan paragraph, replace "until D6b's tests PR they are `pytest.skip` placeholders, so the trace check passes" with "`docs/d6b-pins.md` holds its interfaces and semantics and wins over the interface block below".
  - At the end of §D6, before "Out of scope", add the "Settled in D6a" paragraph (§Settled in D6a below).
- **`spec.md`:**
  - Add the revision line `Revised 2026-11-01 (night) before D6b: Muse Wonder's model ID confirmed (§8); authoring detail in \`docs/d6b-pins.md\`.`
  - In the §8 author row, replace "Muse Wonder via Meta Model API (model ID to confirm on the dashboard)" with "`muse-wonder-1.0` via Meta Model API".
  - In §8 "JSON outside tools", add: "`strict: true` returns 400 on keywords the contract schema uses (`minProperties`), so `strict` is omitted and the contract loader is the real check (D6b P1)."
  - In §8 "Cost", add: "Muse Wonder (authoring, effort `high`): about $0.13 and 48 s for a first version at ~7k input and ~5k output tokens (D6b P1)."
  - In §12 "Open", remove "Muse Wonder's model ID;".
- **New divergence tells:**
  - `grep -c d6a-pins docs/deliverables.md` gives 3, and `grep -c d6b-pins docs/deliverables.md` gives 2;
  - `grep -c d6b-pins docs/spec.md` gives 1, and `grep -c 'model ID to confirm' docs/spec.md` gives 0;
  - the other tells are unchanged.

### Settled in D6a

**Settled in D6a.** [`docs/d6a-pins.md`](d6a-pins.md) records the interfaces and semantics as built (#30 and the two PRs after it). In short:
- Only running a pending segment calls the model. A paused incident resumes on a tripwire change only when a tripwire that was tripped at the pause is now `ok`; a trip while paused just attaches (pin 13).
- The executor checks the approval and the resource version before it journals anything, merges parameters by `displayName` onto the full list, and sends `integrationVersion` on every connector patch. `recover()` follows per-method rules (pins 18, 19).
- Verification checks status, not events; `none` verifies at once if its target is already `ok` (pin 15). The postmortem is built by code from the incident record.
- Scripted segments are generated in the D6 runtime loop with the canary scheduler on (pin 23).
- Accepted MVP limits: incident records are in memory only, and the verifier accepts a target tripwire the incident never tripped.

## Session A goal

```
Objective: Write the D6b acceptance tests (deliverables.md §D6 rows T11, T12, T13, T15, T21)
  exactly as specified in docs/d6b-pins.md.
Inputs: docs/spec.md (§5.1, §8), docs/deliverables.md §1 and §D6, docs/d5-pins.md,
  docs/d6a-pins.md, docs/d6b-pins.md, .agents/AGENTS.md, prompts/author.md,
  fixtures/briefs/alert-ingestion.yaml.
Deliverables:
  - tests/acceptance/d6/: replace the five pytest.skip("D6b") placeholders in place, one function
    per ID, names unchanged (d6b-pins 16-17).
  - tests/acceptance/d6/d6_helpers.py: the d6 helpers moved out of conftest.py; in the D6a test
    files change only the import lines (d6b-pins 20).
  - fixtures/scripted/d6b/*.json (d6b-pins 18).
  - Stubs raising NotImplementedError: fleetsre/authoring/brief.py, schema.py, loop.py (every
    name in d6b-pins 1, 3, 5) and contracts.parse_contract. Leave fleetsre/sre/gateway.py alone.
Pre-flight: run P2, P4 and P5 (d6b-pins §Pre-flight) outside the tests; do not commit those
  scripts.
Rules: no implementation logic. Do not edit docs/, contracts/, prompts/, other fixtures, or other
  deliverables' tests, except the D6a import lines named above.
Acceptance: pytest tests/acceptance/d6 collects; every D6a test still passes; every D6b test except
  T15 fails with NotImplementedError or a missing fixture; T15 skips without MODEL_API_KEY;
  python -m pytest tests/acceptance/d5 tests/acceptance/d6 --co -q collects; make lint passes.
Stop: print a table of test ID -> function name -> current failure reason, then the P2, P4 and
  P5 outputs, then stop.
```
