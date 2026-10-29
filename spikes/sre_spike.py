"""D2 SRE feasibility spike (Session A stub).

Interface (docs/deliverables.md §D2):
  python spikes/sre_spike.py --fixture <dir> [--model {scripted|<model id>}]
      [--runs N] [--gates on|off] [--script <file>]

--model defaults to $FLEETSRE_SPIKE_MODEL, else muse-spark-1.3.
--script defaults to <fixture>/scripted.json.
Stdout per run: one TRACE line per turn, then RESULT, then DIAGNOSIS if accepted.
RESULT is {"run","status","turns","readCalls","refusals","multiCallTurns"}:
"status" is "accepted" iff the run ends with an accepted submission, and
"multiCallTurns" counts turns whose model response held more than one
function_call (only the first call runs).
Exit 0 when the program completes, whatever the run outcomes.

Stub tools read the fixture dir only. incident.json "now" is the current
time: no tool returns anything after it.
Read-tool result contracts:
  query_logs -> {"entries": [...], "total": int, "truncated": bool}
    At most 50 entries, newest first. "total" counts every match at or
    before incident now; entries after now never match. "truncated" is
    true when matches exceed 50. "deployment" matches
    jsonPayload.environment; "window" is a half-open "<start>/<end>" interval
    of ISO-8601 UTC instants; "filter" is "" (all), "severity >= WARNING",
    or "event = <jsonPayload.event>".
  get_resource -> the resource object exactly as in resources.json
  list_connector_logs -> {"logs": [...], "total": int, "truncated": bool}
    At most 50 records, newest first, mirroring the fixture order.
    Records with "time" (unix ms) after incident now are excluded;
    "total" counts the records at or before now.
  test_integration_instance -> {"success": bool, "message": str}
  compare_siblings -> {"window": str, "deployments": {deployment: signals}}
    "window" echoes the request. Per deployment of the service, computed
    from logs.jsonl over the window: {"runs": int, "warningRuns": int,
    "errorRuns": int, "alertsIngested": int, "lastRunAt": str}. "runs"
    counts connector.run.completed entries; warning/errorRuns count runs
    with a WARNING/ERROR entry; alertsIngested counts alert.ingested;
    lastRunAt is the newest completed timestamp. No verdict, only signals.

Minimal gates (inside the working tools): hypotheses before evidence or
submission; 2-4 hypotheses with at least one alternative; a log source's
insertId exists; a tool source's callId is a call made in this run; cited
evidence IDs were recorded. A refused record_evidence call consumes no
evidence ID. A tool source's "field" is a dotted path into that call's
result (e.g. "deployments.alert-ingestion/alpha.warningRuns"); the gate
refuses when the path is missing or its value differs.
"""

from pathlib import Path


class StubTools:
    """Stub tools over one fixture dir (Session A interface stub)."""

    def __init__(self, fixture_dir: str | Path) -> None:
        """Bind to a fixture dir holding logs.jsonl, resources.json, incident.json."""
        raise NotImplementedError

    def query_logs(
        self, deployment: str, window: str, filter: str, rationale: str
    ) -> dict:
        """Return {"entries", "total", "truncated"} from logs.jsonl (cap 50)."""
        raise NotImplementedError

    def get_resource(self, name: str, rationale: str) -> dict:
        """Return the named resource exactly as in resources.json."""
        raise NotImplementedError

    def list_connector_logs(
        self, connectorInstance: str, rationale: str
    ) -> dict:
        """Return {"logs", "total", "truncated"} for the instance (cap 50)."""
        raise NotImplementedError

    def test_integration_instance(self, name: str, rationale: str) -> dict:
        """Return {"success", "message"} for the integration instance."""
        raise NotImplementedError

    def compare_siblings(
        self, service: str, component: str, window: str, rationale: str
    ) -> dict:
        """Return {"window", "deployments"} signals from logs.jsonl."""
        raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (Session A stub)."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
