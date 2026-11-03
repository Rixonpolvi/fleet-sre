"""Keyless GCP identities via impersonation (docs/d7-pins.md §Interfaces 1).

No service account ever gets a key. The caller's Application Default
Credentials impersonate a D7 service account for one hour at a time.
"""

import google.auth
import google.auth.credentials
from google.auth import impersonated_credentials

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
IMPERSONATION_LIFETIME_S = 3600


def service_account_email(name: str, project: str) -> str:
    """Return `<name>@<project>.iam.gserviceaccount.com` (d7-pins 1)."""
    return f"{name}@{project}.iam.gserviceaccount.com"


def impersonated(
    name: str, project: str, *, scopes: list[str] | None = None
) -> google.auth.credentials.Credentials:
    """Impersonate a D7 service account from ADC (d7-pins 1).

    Builds ``impersonated_credentials.Credentials`` with the cloud-platform
    scope and a 3600 s lifetime. No key file is read, and the credentials
    are not refreshed here; callers refresh before use (d7-pins 5).
    """
    target_scopes = list(scopes) if scopes is not None else [CLOUD_PLATFORM_SCOPE]
    source, _ = google.auth.default(scopes=target_scopes)
    return impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=service_account_email(name, project),
        target_scopes=target_scopes,
        lifetime=IMPERSONATION_LIFETIME_S,
    )
