"""Connector runtime helpers (mocks.md §3.2).

Severity map follows Google's priority scale for custom connectors:
Informative -1, Low 40, Medium 60, High 80, Critical 100.
https://docs.cloud.google.com/chronicle/docs/soar/respond/ide/creating-a-custom-connector
"""

# invented: passing the raw severity string through when it is unmapped and the
# Unknown Severity Priority parameter is empty (the severity-drift bug).


def map_priority(severity: str, unknown_severity_priority: str) -> int | str:
    """Map a Sentinel severity to an AlertInfo priority.

    Known severities map to Google's scale. An unmapped severity maps to the
    Unknown Severity Priority parameter when set, else passes through raw.
    """
    known = {"High": 80, "Medium": 60, "Low": 40, "Informational": -1}
    if severity in known:
        return known[severity]
    if unknown_severity_priority != "":
        try:
            return int(unknown_severity_priority)
        except ValueError:
            return unknown_severity_priority
    return severity
