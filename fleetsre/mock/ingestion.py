"""Ingestion mapping: Sentinel incident -> AlertInfo (mocks.md §3.2-§3.3).

AlertInfo field names and the priority scale are faithful:
https://docs.cloud.google.com/chronicle/docs/soar/respond/ide/creating-a-custom-connector
"""

# invented: rule_generator fallback, canary flag, jsonPayload shape.

from typing import Any

from fleetsre.mock.connector import map_priority


def incident_to_alert(
    incident: dict[str, Any], unknown_severity_priority: str
) -> dict[str, Any]:
    """Map one upstream incident to the alert stored on alert.ingested entries."""
    props = incident["properties"]
    labels = props.get("labels") or []
    first = props["firstActivityTimeUtc"]
    last = props["lastActivityTimeUtc"]
    return {
        "name": props["title"],
        "ticket_id": props["providerIncidentId"],
        "rule_generator": labels[0] if labels else "Sentinel Analytics",
        "priority": map_priority(props["severity"], unknown_severity_priority),
        "device_vendor": "Microsoft",
        "device_product": "Azure Sentinel",
        "environment": "",
        "start_time": int(first.timestamp() * 1000),
        "end_time": int(last.timestamp() * 1000),
        "canary": bool(props.get("canary", False)),
    }
