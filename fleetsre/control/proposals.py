"""Proposals (d6a-pins 3). Stub: behavior raises."""

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
    raise NotImplementedError


def proposal_hash(
    incident_id: str,
    seq: int,
    method: str,
    resource: str,
    update_mask: list[str],
    patch: dict[str, Any],
    resource_version: str,
) -> str:
    raise NotImplementedError
