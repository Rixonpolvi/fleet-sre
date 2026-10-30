"""Fake Sentinel upstream: one workspace per tenant (mocks.md §3.1).

Payload shape follows Microsoft Sentinel Incidents - List (faithful fields:
name, type, properties.title/severity/status/incidentNumber/providerIncidentId,
labels, timestamps).
https://learn.microsoft.com/en-us/rest/api/securityinsights/incidents/list
"""

# invented: the transport, the token endpoint, Poisson arrivals, incident titles.

import hashlib
import random
from datetime import datetime, timedelta
from typing import Any

SEVERITIES = ("High", "Medium", "Low", "Informational")

# invented: title templates for synthetic incidents.
_TITLES = (
    "Suspicious sign-in",
    "Impossible travel alert",
    "Malware detected on host",
    "Anomalous data exfiltration",
    "Brute force attempt",
    "Suspicious PowerShell activity",
)

# invented: label pool; rule_generator falls back to Sentinel Analytics.
_LABELS = ("Sentinel Analytics", "Threat Protection", "Insider Risk")


def seeded_rng(seed: int, stream: str) -> random.Random:
    """One deterministic RNG per stream, derived from (seed, stream name)."""
    digest = hashlib.sha256(f"{seed}:{stream}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest, "big"))


class _Workspace:
    """Arrival state for one tenant workspace."""

    def __init__(self, rate_per_hour: float, rng: random.Random, start: datetime):
        self.rate_per_hour = rate_per_hour
        self.rng = rng
        self.next_arrival: datetime | None = None
        self.seq = 0
        self.pending: list[dict[str, Any]] = []
        if rate_per_hour > 0:
            self.next_arrival = start + timedelta(
                seconds=rng.expovariate(rate_per_hour / 3600.0)
            )

    def generate_until(
        self,
        upto: datetime,
        env: str,
        drift_rate: float,
    ) -> None:
        """Append arrivals up to `upto`. F3 turns a fraction into Critical."""
        while self.next_arrival is not None and self.next_arrival <= upto:
            at = self.next_arrival
            self.seq += 1
            if drift_rate > 0 and self.rng.random() < drift_rate:
                severity = "Critical"
            else:
                roll = self.rng.random()
                if roll < 0.25:
                    severity = "High"
                elif roll < 0.60:
                    severity = "Medium"
                elif roll < 0.85:
                    severity = "Low"
                else:
                    severity = "Informational"
            title = self.rng.choice(_TITLES)
            labels = [self.rng.choice(_LABELS)] if self.rng.random() < 0.7 else []
            ticket = f"{env}-inc-{self.seq:06d}"
            span = timedelta(seconds=self.rng.randint(0, 300))
            self.pending.append(
                {
                    "name": ticket,
                    "type": "Microsoft.SecurityInsights/incidents",
                    "properties": {
                        "title": f"{title} {ticket}",
                        "description": f"Synthetic incident {ticket}",
                        "severity": severity,
                        "status": "New",
                        "incidentNumber": self.seq,
                        "createdTimeUtc": at,
                        "firstActivityTimeUtc": at,
                        "lastActivityTimeUtc": at + span,
                        "lastModifiedTimeUtc": at + span,
                        "providerName": "Sentinel",
                        "providerIncidentId": ticket,
                        "labels": labels,
                        "canary": False,
                    },
                }
            )
            gap = self.rng.expovariate(self.rate_per_hour / 3600.0)
            self.next_arrival = at + timedelta(seconds=gap)


class FakeSentinel:
    """In-process upstream; connectors call it as functions, never over HTTP."""

    def __init__(self, seed: int, start: datetime, rates: dict[str, float]):
        self._workspaces = {
            env: _Workspace(rate, seeded_rng(seed, f"arrivals:{env}"), start)
            for env, rate in rates.items()
        }

    def workspace_ids(self) -> list[str]:
        return sorted(self._workspaces)

    def has(self, env: str) -> bool:
        return env in self._workspaces

    def generate_until(self, env: str, upto: datetime, drift_rate: float) -> None:
        self._workspaces[env].generate_until(upto, env, drift_rate)

    def fetch(self, env: str) -> list[dict[str, Any]]:
        """Drain pending incidents (dedupe by providerIncidentId)."""
        pending = self._workspaces[env].pending
        self._workspaces[env].pending = []
        seen: set[str] = set()
        out = []
        for incident in pending:
            ticket = incident["properties"]["providerIncidentId"]
            if ticket not in seen:
                seen.add(ticket)
                out.append(incident)
        return out

    def plant_canary(self, env: str, provider_id: str, at: datetime) -> None:
        """Queue a canary incident; always Informational so F3 never affects it."""
        self._workspaces[env].pending.append(
            {
                "name": provider_id,
                "type": "Microsoft.SecurityInsights/incidents",
                "properties": {
                    "title": "[CANARY] synthetic incident",
                    "description": "Synthetic canary incident",
                    "severity": "Informational",
                    "status": "New",
                    "incidentNumber": 0,
                    "createdTimeUtc": at,
                    "firstActivityTimeUtc": at,
                    "lastActivityTimeUtc": at,
                    "lastModifiedTimeUtc": at,
                    "providerName": "Sentinel",
                    "providerIncidentId": provider_id,
                    "labels": [],
                    "canary": True,
                },
            }
        )
