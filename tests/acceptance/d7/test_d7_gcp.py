"""D7 acceptance tests: Cloud Logging adapters and the IAM air gap.

Covers deliverables.md §D7 rows T1, T7, T8 and T10, exactly as specified
in docs/d7-pins.md (Rows, pins 5-8). Every test is marked gcp and skips
when FLEETSRE_GCP_PROJECT is unset. Google packages are imported inside
the test functions, after the first D7 stub call, so with stubs in place
each test fails with NotImplementedError and needs neither infra nor
credentials (d7-pins 5).
"""

import ast
import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SAMPLE_FILES = ("connector.jsonl", "etl.jsonl")
SUBSCRIPTION_ID = "fleet-sre-logs-obs"
READ_DEADLINE_S = 90
PULL_DEADLINE_S = 120
REFRESH_DEADLINE_S = 120
POLL_SLEEP_S = 2
T7_REWRITE_S = 15


def _tagged_samples(now: datetime) -> tuple[str, list[Any]]:
    """All connector+etl samples retagged with a fresh runId (d7-pins 5)."""
    from fleetsre.logmodel import read_jsonl

    run_id = f"d7-{uuid.uuid4().hex}"
    entries: list[Any] = []
    seq = 0
    for name in SAMPLE_FILES:
        path = ROOT / "fixtures" / "logs" / "samples" / name
        if not path.is_file():
            raise FileNotFoundError(f"missing sample fixture: {path}")
        for entry in read_jsonl(path):
            seq += 1
            entry.timestamp = now
            entry.insertId = f"{run_id}-{seq}"
            entry.jsonPayload["runId"] = run_id
            entries.append(entry)
    return run_id, entries


def _refresh(creds: Any) -> None:
    """Refresh impersonated creds, retrying RefreshError to 120 s (d7-pins 5)."""
    import google.auth.exceptions
    import google.auth.transport.requests

    request = google.auth.transport.requests.Request()
    start = time.monotonic()
    while True:
        try:
            creds.refresh(request)
            return
        except google.auth.exceptions.RefreshError:
            if time.monotonic() - start >= REFRESH_DEADLINE_S:
                raise
            time.sleep(POLL_SLEEP_S)


def _retry_denied(call: Callable[[], Any], deadline_s: int) -> Any:
    """Run a positive call, retrying PermissionDenied to deadline (d7-pins 6)."""
    import google.api_core.exceptions

    start = time.monotonic()
    while True:
        try:
            return call()
        except google.api_core.exceptions.PermissionDenied:
            if time.monotonic() - start >= deadline_s:
                raise
            time.sleep(POLL_SLEEP_S)


def _poll_entries(
    query: Any, start: datetime, end: datetime, want: set[str]
) -> dict[str, Any]:
    """Poll query.entries until every wanted insertId is back (d7-pins 5-6)."""
    import google.api_core.exceptions

    deadline = time.monotonic() + READ_DEADLINE_S
    seen: dict[str, Any] = {}
    while True:
        try:
            found = query.entries(start, end)
        except google.api_core.exceptions.PermissionDenied:
            if time.monotonic() >= deadline:
                raise
            time.sleep(POLL_SLEEP_S)
            continue
        seen = {entry.insertId: entry for entry in found}
        if all(insert_id in seen for insert_id in want):
            return seen
        if time.monotonic() >= deadline:
            missing = sorted(set(want) - set(seen))
            raise AssertionError(f"timed out waiting for log read-back: {missing}")
        time.sleep(POLL_SLEEP_S)


def _local_mock_clients() -> tuple[Any, Any, str]:
    """Local mock app with dev auth: obs-reader, deployer, connector name."""
    from fastapi.testclient import TestClient

    from fleetsre.adapters.chronicle_http import ChronicleHttpClient
    from fleetsre.clock import SimClock
    from fleetsre.contracts import load_contract
    from fleetsre.mock.chronicle_api import create_app
    from fleetsre.mock.fleet import load_fleet
    from fleetsre.mock.world import World
    from fleetsre.ports import MemorySink
    from fleetsre.registry import deployments_from_fleet

    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    contract = load_contract(ROOT / "contracts" / "alert-ingestion.json")
    resource = deployments_from_fleet(fleet, contract)[0].resource_names["connector"]
    clock = SimClock(datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc))
    world = World(fleet, 7, clock, MemorySink())
    http = TestClient(create_app(world, dev_auth=True))
    return ChronicleHttpClient(http, "obs-reader"), ChronicleHttpClient(http, "deployer"), resource


def _is_banned_import(module: str) -> bool:
    return module in ("google", "fleetsre.adapters") or module.startswith(
        ("google.", "fleetsre.adapters.")
    )


def _assert_sre_has_no_credential_imports() -> None:
    """No file under fleetsre/sre/ imports google or fleetsre.adapters (D7-T8)."""
    root = ROOT / "fleetsre" / "sre"
    bad: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_banned_import(alias.name):
                        bad.append(f"{path.relative_to(ROOT)}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if _is_banned_import(node.module or ""):
                    bad.append(f"{path.relative_to(ROOT)}: from {node.module} import ...")
    assert not bad, "SRE runtime imports credentials: " + "; ".join(bad)


def _assert_no_service_account_keys() -> None:
    """No repo file is a service-account key JSON; skips .git/,.venv/ (D7-T8)."""
    skip = {".git", ".venv"}
    bad: list[str] = []
    for path in sorted(ROOT.rglob("*.json")):
        if any(part in skip for part in path.relative_to(ROOT).parts):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(data, dict)
            and data.get("type") == "service_account"
            and "private_key" in data
        ):
            bad.append(str(path.relative_to(ROOT)))
    assert not bad, f"service-account keys committed: {bad}"


@pytest.mark.gcp
def test_d7_t1_cloud_logging_write_read() -> None:
    """D7-T1: CloudLoggingSink entries come back from CloudLoggingQuery intact."""
    project = os.environ.get("FLEETSRE_GCP_PROJECT")
    if not project:
        pytest.skip("FLEETSRE_GCP_PROJECT is unset")
    from fleetsre.adapters.cloud_logging import CloudLoggingQuery, CloudLoggingSink
    from fleetsre.adapters.gcp_auth import impersonated

    writer_creds = impersonated("mock-soar", project)
    reader_creds = impersonated("obs-reader", project)
    _refresh(writer_creds)
    _refresh(reader_creds)

    now = datetime.now(timezone.utc)
    run_id, entries = _tagged_samples(now)
    start = now - timedelta(minutes=10)
    end = now + timedelta(minutes=1)
    sink = CloudLoggingSink(project, credentials=writer_creds)
    for entry in entries:
        _retry_denied(lambda e=entry: sink.write(e), PULL_DEADLINE_S)
    query = CloudLoggingQuery(
        project, credentials=reader_creds, extra_filter=f'jsonPayload.runId="{run_id}"'
    )
    want = {entry.insertId for entry in entries}
    seen = _poll_entries(query, start, end, want)
    written = {entry.insertId: entry for entry in entries}
    six = {
        "project_id",
        "location",
        "cluster_name",
        "namespace_name",
        "pod_name",
        "container_name",
    }
    for insert_id in want:
        read = seen[insert_id]
        sent = written[insert_id]
        assert read.resource.type == "k8s_container"
        assert six <= set(read.resource.labels)
        assert read.resource.labels["project_id"] == project
        for label in six - {"project_id"}:
            assert read.resource.labels[label] == sent.resource.labels[label]
        assert read.labels == sent.labels
        assert read.severity == sent.severity
        assert read.jsonPayload == sent.jsonPayload
        assert read.insertId == sent.insertId
    one = next(iter(want))
    got = _retry_denied(lambda: query.get(one), READ_DEADLINE_S)
    assert got is not None
    assert got.insertId == one
    assert got.jsonPayload.get("runId") == run_id


@pytest.mark.gcp
def test_d7_t7_iam_positive_paths() -> None:
    """D7-T7: each identity completes its own path (GCP plus local mock app)."""
    project = os.environ.get("FLEETSRE_GCP_PROJECT")
    if not project:
        pytest.skip("FLEETSRE_GCP_PROJECT is unset")
    from fleetsre.adapters.cloud_logging import CloudLoggingQuery, CloudLoggingSink
    from fleetsre.adapters.gcp_auth import impersonated

    writer_creds = impersonated("mock-soar", project)
    reader_creds = impersonated("obs-reader", project)
    _refresh(writer_creds)
    _refresh(reader_creds)

    import google.api_core.exceptions
    from google.cloud import pubsub_v1

    now = datetime.now(timezone.utc)
    run_id, entries = _tagged_samples(now)
    first, rest = entries[0], entries[1:]
    sink = CloudLoggingSink(project, credentials=writer_creds)
    _retry_denied(lambda: sink.write(first), PULL_DEADLINE_S)
    query = CloudLoggingQuery(
        project, credentials=reader_creds, extra_filter=f'jsonPayload.runId="{run_id}"'
    )
    start = now - timedelta(minutes=10)
    end = now + timedelta(minutes=1)
    _poll_entries(query, start, end, {first.insertId})

    subscriber = pubsub_v1.SubscriberClient(credentials=reader_creds)
    subscription = f"projects/{project}/subscriptions/{SUBSCRIPTION_ID}"
    own_ids = {entry.insertId for entry in entries}
    pending = list(rest)
    deadline = time.monotonic() + PULL_DEADLINE_S
    last_write = time.monotonic()
    pulled = False
    while not pulled:
        if pending and time.monotonic() - last_write >= T7_REWRITE_S:
            extra = pending.pop(0)
            extra.timestamp = datetime.now(timezone.utc)
            _retry_denied(lambda e=extra: sink.write(e), PULL_DEADLINE_S)
            last_write = time.monotonic()
        try:
            response = subscriber.pull(
                request={"subscription": subscription, "max_messages": 10},
                timeout=10,
            )
        except google.api_core.exceptions.PermissionDenied:
            if time.monotonic() >= deadline:
                raise
            time.sleep(POLL_SLEEP_S)
            continue
        except google.api_core.exceptions.DeadlineExceeded:
            # An empty subscription hits the client timeout: no messages.
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for Pub/Sub delivery")
            time.sleep(POLL_SLEEP_S)
            continue
        received = list(response.received_messages)
        if received:
            subscriber.acknowledge(
                request={
                    "subscription": subscription,
                    "ack_ids": [message.ack_id for message in received],
                }
            )
        for message in received:
            try:
                payload = json.loads(message.message.data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if payload.get("insertId") in own_ids:
                pulled = True
                break
        if not pulled:
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for Pub/Sub delivery")
            time.sleep(POLL_SLEEP_S)

    obs, deployer, resource = _local_mock_clients()
    seen = obs.get(resource)
    assert seen["name"] == resource
    current = deployer.get(resource)
    patched = deployer.patch(
        resource,
        {
            "enabled": current["enabled"],
            "integrationVersion": current["integrationVersion"],
        },
        ["enabled"],
    )
    assert patched["name"] == resource


@pytest.mark.gcp
def test_d7_t8_iam_negative_paths() -> None:
    """D7-T8: denied paths raise PermissionDenied; the SRE holds no credentials."""
    project = os.environ.get("FLEETSRE_GCP_PROJECT")
    if not project:
        pytest.skip("FLEETSRE_GCP_PROJECT is unset")
    from fleetsre.adapters.cloud_logging import CloudLoggingQuery, CloudLoggingSink
    from fleetsre.adapters.gcp_auth import impersonated

    writer_creds = impersonated("mock-soar", project)
    reader_creds = impersonated("obs-reader", project)
    deployer_creds = impersonated("deployer", project)
    _refresh(writer_creds)
    _refresh(reader_creds)
    _refresh(deployer_creds)

    import google.api_core.exceptions
    from google.cloud import pubsub_v1

    from fleetsre.ports import ChronicleError

    now = datetime.now(timezone.utc)
    run_id, entries = _tagged_samples(now)
    control, probe = entries[0], entries[1]
    start = now - timedelta(minutes=10)
    end = now + timedelta(minutes=1)

    writer_sink = CloudLoggingSink(project, credentials=writer_creds)
    _retry_denied(lambda: writer_sink.write(control), PULL_DEADLINE_S)
    reader_sink = CloudLoggingSink(project, credentials=reader_creds)
    with pytest.raises(google.api_core.exceptions.PermissionDenied):
        reader_sink.write(probe)

    extra = f'jsonPayload.runId="{run_id}"'
    reader_query = CloudLoggingQuery(project, credentials=reader_creds, extra_filter=extra)
    _poll_entries(reader_query, start, end, {control.insertId})
    writer_query = CloudLoggingQuery(project, credentials=writer_creds, extra_filter=extra)
    with pytest.raises(google.api_core.exceptions.PermissionDenied):
        writer_query.entries(start, end)

    subscription = f"projects/{project}/subscriptions/{SUBSCRIPTION_ID}"

    def _pull(creds: Any) -> None:
        client = pubsub_v1.SubscriberClient(credentials=creds)
        try:
            response = client.pull(
                request={"subscription": subscription, "max_messages": 10},
                timeout=10,
            )
        except google.api_core.exceptions.DeadlineExceeded:
            # An empty subscription hits the client timeout: no messages.
            return
        ack_ids = [message.ack_id for message in response.received_messages]
        if ack_ids:
            client.acknowledge(request={"subscription": subscription, "ack_ids": ack_ids})

    _retry_denied(lambda: _pull(reader_creds), PULL_DEADLINE_S)
    with pytest.raises(google.api_core.exceptions.PermissionDenied):
        _pull(deployer_creds)

    obs, deployer, resource = _local_mock_clients()
    current = deployer.get(resource)
    with pytest.raises(ChronicleError) as denied:
        obs.patch(
            resource,
            {
                "enabled": current["enabled"],
                "integrationVersion": current["integrationVersion"],
            },
            ["enabled"],
        )
    assert denied.value.status == 403
    assert denied.value.body.get("status") == "PERMISSION_DENIED"

    _assert_sre_has_no_credential_imports()
    _assert_no_service_account_keys()


@pytest.mark.gcp
def test_d7_t10_no_user_managed_keys() -> None:
    """D7-T10: the three D7 accounts exist and no account has user-managed keys."""
    project = os.environ.get("FLEETSRE_GCP_PROJECT")
    if not project:
        pytest.skip("FLEETSRE_GCP_PROJECT is unset")
    from fleetsre.adapters.gcp_auth import service_account_email

    emails = [
        service_account_email(name, project)
        for name in ("mock-soar", "obs-reader", "deployer")
    ]

    from google.cloud import iam_admin_v1
    from google.cloud.iam_admin_v1.types import ListServiceAccountKeysRequest

    client = iam_admin_v1.IAMClient()
    accounts = list(client.list_service_accounts(name=f"projects/{project}"))
    have = {account.email for account in accounts}
    for email in emails:
        assert email in have, f"missing service account {email}"
    user_managed = ListServiceAccountKeysRequest.KeyType.USER_MANAGED
    offenders: list[str] = []
    for account in accounts:
        keys = client.list_service_account_keys(name=account.name)
        if any(key.key_type == user_managed for key in keys.keys):
            offenders.append(account.email)
    assert not offenders, f"user-managed keys on: {sorted(offenders)}"
