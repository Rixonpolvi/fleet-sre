"""Proposals: versioned patch intents awaiting approval (d6a-pins 3)."""

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class Proposal(BaseModel):
    incident_id: str
    seq: int
    hash: str
    method: str
    resource: str
    update_mask: list[str]
    patch: dict[str, Any]
    resource_version: str
    diagnosis: dict[str, Any]
    created_at: datetime


def resource_version(resource: dict[str, Any]) -> str:
    """Concurrency token: connector `version`, job `updateTime` (pin 3)."""
    name = str(resource.get("name", ""))
    if "/connectorInstances/" in name:
        return str(resource["version"])
    if "/jobInstances/" in name:
        return str(resource["updateTime"])
    raise ValueError(f"unknown resource kind: {name!r}")


def proposal_hash(
    incident_id: str,
    seq: int,
    method: str,
    resource: str,
    update_mask: list[str],
    patch: dict[str, Any],
    resource_version: str,
) -> str:
    """SHA-256 hex over the proposal's identity fields (pin 3)."""
    payload = {
        "incidentId": incident_id,
        "seq": seq,
        "method": method,
        "resource": resource,
        "updateMask": update_mask,
        "patch": patch,
        "resourceVersion": resource_version,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
