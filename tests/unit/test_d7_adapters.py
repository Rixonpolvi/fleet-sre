"""D7 unit tests (docs/d7-pins.md §Unit tests): converters, filters, email."""

import copy
from datetime import datetime, timezone
from pathlib import Path

from fleetsre.adapters.cloud_logging import (
    build_filter,
    build_get_filter,
    from_cloud_entry,
    to_cloud_entry,
)
from fleetsre.adapters.gcp_auth import service_account_email
from fleetsre.logmodel import read_jsonl

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "unit-test-project"
BASE = (
    'resource.type="k8s_container"'
    ' AND resource.labels.namespace_name="chronicle-soar"'
)


def test_cloud_entry_round_trip_every_sample_line():
    paths = sorted((ROOT / "fixtures" / "logs" / "samples").glob("*.jsonl"))
    assert paths, "no sample fixtures"
    count = 0
    for path in paths:
        for entry in read_jsonl(path):
            count += 1
            before = entry.model_dump(mode="json")
            data = to_cloud_entry(entry, PROJECT)
            assert entry.model_dump(mode="json") == before
            assert data["logName"] == f"projects/{PROJECT}/logs/chronicle-soar"
            assert data["resource"]["labels"]["project_id"] == PROJECT
            snapshot = copy.deepcopy(data)
            back = from_cloud_entry(data)
            assert data == snapshot
            assert back.insertId == entry.insertId
            assert back.timestamp == entry.timestamp
            assert back.severity == entry.severity
            assert back.labels == entry.labels
            assert back.jsonPayload == entry.jsonPayload
            assert back.resource.type == entry.resource.type
            assert back.logName == f"projects/{PROJECT}/logs/chronicle-soar"
            want_labels = dict(entry.resource.labels)
            want_labels["project_id"] = PROJECT
            assert back.resource.labels == want_labels
    assert count > 0


def test_build_filter_exact():
    start = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 29, 15, 0, 0, tzinfo=timezone.utc)
    assert build_filter(start, end) == (
        f"{BASE}"
        ' AND timestamp>="2026-10-29T14:00:00Z"'
        ' AND timestamp<"2026-10-29T15:00:00Z"'
    )


def test_build_filter_extra_and_microseconds():
    start = datetime(2026, 10, 29, 14, 0, 0, 123456, tzinfo=timezone.utc)
    end = datetime(2026, 10, 29, 14, 1, 0, tzinfo=timezone.utc)
    assert build_filter(start, end, 'jsonPayload.runId="d7-abc"') == (
        f"{BASE}"
        ' AND timestamp>="2026-10-29T14:00:00.123456Z"'
        ' AND timestamp<"2026-10-29T14:01:00Z"'
        ' AND jsonPayload.runId="d7-abc"'
    )


def test_build_filter_naive_is_utc():
    start = datetime(2026, 10, 29, 14, 0, 0)
    end = datetime(2026, 10, 29, 14, 1, 0)
    assert build_filter(start, end) == (
        f"{BASE}"
        ' AND timestamp>="2026-10-29T14:00:00Z"'
        ' AND timestamp<"2026-10-29T14:01:00Z"'
    )


def test_build_get_filter_exact():
    assert build_get_filter("d7-abc-1") == f'{BASE} AND insertId="d7-abc-1"'


def test_service_account_email():
    assert service_account_email("mock-soar", "p") == (
        "mock-soar@p.iam.gserviceaccount.com"
    )
    assert service_account_email("obs-reader", "p") == (
        "obs-reader@p.iam.gserviceaccount.com"
    )
    assert service_account_email("deployer", "p") == (
        "deployer@p.iam.gserviceaccount.com"
    )
