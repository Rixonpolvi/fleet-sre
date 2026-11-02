"""Keyless GCP identities via impersonation (docs/d7-pins.md §Interfaces 1).

Stub for the D7 tests PR: every name raises NotImplementedError.
This PR adds the google dependencies; Session B implements the names.
"""

from typing import Any


def service_account_email(name: str, project: str) -> str:
    """Return `<name>@<project>.iam.gserviceaccount.com` (d7-pins 1)."""
    raise NotImplementedError


def impersonated(
    name: str, project: str, *, scopes: list[str] | None = None
) -> Any:
    """Impersonate a D7 service account with ADC (d7-pins 1).

    Session B returns ``google.auth.credentials.Credentials``; the stub
    annotates ``Any`` so this module imports no google package.
    """
    raise NotImplementedError
