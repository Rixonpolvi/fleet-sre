"""D1 acceptance tests: contracts, log contract, fleet and scenarios."""

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from fleetsre.contracts import ContractError, load_contract
from fleetsre.harness.scenario import load_scenario
from fleetsre.logmodel import LogEntry, read_jsonl
from fleetsre.mock.fleet import load_fleet

ROOT = Path(__file__).resolve().parents[3]


def _find_param_refs(value: Any, pattern: re.Pattern[str]) -> list[str]:
    if isinstance(value, str):
        return pattern.findall(value)
    if isinstance(value, dict):
        refs: list[str] = []
        for item in value.values():
            refs.extend(_find_param_refs(item, pattern))
        return refs
    if isinstance(value, list):
        refs = []
        for item in value:
            refs.extend(_find_param_refs(item, pattern))
        return refs
    return []


def test_d1_t1_contracts_validate():
    """D1-T1: Both contracts validate and load via load_contract."""
    schema = json.loads((ROOT / "contracts" / "schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    for service in ("alert-ingestion", "intel-sync"):
        data = json.loads((ROOT / "contracts" / f"{service}.json").read_text())
        validator.validate(data)
        contract = load_contract(ROOT / "contracts" / f"{service}.json")
        assert contract.service == service
        assert contract.version == "1.0.0"
        assert len(contract.tripwires) >= 1


def test_d1_t2_referential_integrity():
    """D1-T2: Components, params, IDs and overrides are referentially intact."""
    pattern = re.compile(r"\$\{([A-Za-z][A-Za-z0-9]*)\}")
    for service in ("alert-ingestion", "intel-sync"):
        contract = load_contract(ROOT / "contracts" / f"{service}.json")
        component_ids = {c.id for c in contract.components}
        tripwire_ids = [t.id for t in contract.tripwires]
        assert len(set(tripwire_ids)) == len(tripwire_ids)
        for tripwire in contract.tripwires:
            assert tripwire.component in component_ids
            for ref in _find_param_refs(tripwire.spec, pattern):
                assert ref in contract.parameters
        for key in contract.changePoint.overrides:
            assert key in tripwire_ids


def test_d1_t3_enumerations():
    """D1-T3: Component kinds and tripwire types are within the enums."""
    kinds = {"connectorInstance", "etlFlow", "jobInstance"}
    types = {"cadence", "synthetic", "schema", "errorSignature"}
    for service in ("alert-ingestion", "intel-sync"):
        contract = load_contract(ROOT / "contracts" / f"{service}.json")
        for component in contract.components:
            assert component.kind in kinds
        for tripwire in contract.tripwires:
            assert tripwire.type in types


def test_d1_t4_invalid_rejected():
    """D1-T4: Invalid contracts raise ContractError with the expected code."""
    codes = {
        "schema_invalid",
        "unknown_component",
        "duplicate_tripwire",
        "dangling_parameter",
        "unknown_override",
        "unknown_override_field",
        "threshold_exceeds_window",
        "container_kind_mismatch",
    }
    files = sorted((ROOT / "fixtures" / "invalid").glob("*.json"))
    assert len(files) >= 5
    seen: set[str] = set()
    for path in files:
        expected = json.loads(path.read_text())["_expectError"]
        assert expected in codes
        seen.add(expected)
        with pytest.raises(ContractError) as excinfo:
            load_contract(path)
        assert excinfo.value.code == expected
    assert "unknown_component" in seen
    assert "schema_invalid" in seen
    assert "dangling_parameter" in seen
    assert "duplicate_tripwire" in seen
    assert "unknown_override" in seen or "unknown_override_field" in seen


def test_d1_t5_required_tripwires():
    """D1-T5: Required tripwires exist with the specified shapes."""
    alert = load_contract(ROOT / "contracts" / "alert-ingestion.json")
    by_id = {t.id: t for t in alert.tripwires}
    assert {"dead-connector", "canary", "priority-shape", "connector-errors"} <= set(by_id)
    assert by_id["dead-connector"].type == "cadence"
    assert by_id["canary"].type == "synthetic"
    assert by_id["priority-shape"].type == "schema"
    assert by_id["connector-errors"].type == "errorSignature"
    intel = load_contract(ROOT / "contracts" / "intel-sync.json")
    intel_by_id = {t.id: t for t in intel.tripwires}
    assert intel_by_id["dead-job"].type == "cadence"
    assert intel_by_id["job-errors"].type == "errorSignature"
    hollow = intel_by_id["hollow-run"]
    assert hollow.type == "schema"
    assert hollow.spec["field"] == "jsonPayload.indicatorsSynced"
    assert hollow.spec["minimum"] == 1
    assert hollow.spec["consecutive"] >= 2


def test_d1_t6_sample_logs_parse():
    """D1-T6: Every sample log line parses as LogEntry."""
    files = sorted((ROOT / "fixtures" / "logs" / "samples").glob("*.jsonl"))
    assert len(files) >= 1
    for path in files:
        entries = read_jsonl(path)
        raw = [line for line in path.read_text().splitlines() if line.strip()]
        assert len(entries) == len(raw)
        for entry in entries:
            assert isinstance(entry, LogEntry)
            assert entry.insertId


def test_d1_t7_faithful_labels():
    """D1-T7: Sample and golden entries carry faithful labels."""
    connector_labels = (
        "integration_name",
        "integration_version",
        "connector_name",
        "connector_instance",
    )
    job_labels = ("integration_name", "integration_version", "job_name")
    files = sorted((ROOT / "fixtures" / "logs" / "samples").glob("*.jsonl"))
    files += sorted((ROOT / "fixtures" / "golden").glob("*.jsonl"))
    assert len(files) >= 1
    for path in files:
        entries = read_jsonl(path)
        ids = [entry.insertId for entry in entries]
        assert len(set(ids)) == len(ids)
        for entry in entries:
            assert entry.resource.type == "k8s_container"
            assert entry.resource.labels["namespace_name"] == "chronicle-soar"
            assert entry.resource.labels["container_name"] in {"python", "etl"}
            event = entry.jsonPayload.get("event", "")
            assert isinstance(event, str) and event
            if event.startswith("connector."):
                for label in connector_labels:
                    assert label in entry.labels
            elif event.startswith("job."):
                for label in job_labels:
                    assert label in entry.labels
            elif event.startswith("alert."):
                assert "correlation_id" in entry.labels
            else:
                pytest.fail(f"unexpected event {event!r} in {path.name}")


def test_d1_t8_fleet_config():
    """D1-T8: Fleet loads with expected environments, volumes and intervals."""
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    assert set(fleet.environments) == {"alpha", "bravo", "charlie", "canary"}
    volumes = {"alpha": 120, "bravo": 30, "charlie": 2, "canary": 0}
    for env, volume in volumes.items():
        params = fleet.environments[env].deployments["alert-ingestion"].parameters
        assert params["expectedAlertsPerHour"] == volume
        assert params["intervalSeconds"] == 60
    for env in ("alpha", "bravo"):
        params = fleet.environments[env].deployments["intel-sync"].parameters
        assert params["intervalSeconds"] == 300
    for env in ("charlie", "canary"):
        assert "intel-sync" not in fleet.environments[env].deployments


def test_d1_t9_scenario_library_complete():
    """D1-T9: Every required scenario ID exists for its service."""
    required = {
        "alert-ingestion": [
            "clean",
            "stall",
            "green-liar",
            "severity-drift",
            "flap-mild",
            "flap-severe",
            "recovery",
            "quiet-tenant",
        ],
        "intel-sync": ["clean", "job-stall", "hollow-success", "job-error", "recovery"],
    }
    for service, ids in required.items():
        for sid in ids:
            path = ROOT / "fixtures" / "scenarios" / service / f"{sid}.yaml"
            assert path.exists(), f"missing {service}/{sid}"
            scenario = load_scenario(path)
            assert scenario.id == sid
            assert scenario.service == service


def test_d1_t10_scenario_validity():
    """D1-T10: Scenarios load with valid faults, envs and tripwire refs."""
    valid_faults = {
        "connector_stall",
        "credential_expiry",
        "severity_drift",
        "job_hollow_success",
        "job_error",
        "connector_flap",
        "job_stall",
    }
    fleet = load_fleet(ROOT / "fixtures" / "fleet.yaml")
    tripwire_ids = {}
    for service in ("alert-ingestion", "intel-sync"):
        contract = load_contract(ROOT / "contracts" / f"{service}.json")
        tripwire_ids[service] = {t.id for t in contract.tripwires}
    files = sorted((ROOT / "fixtures" / "scenarios").rglob("*.yaml"))
    assert len(files) >= 1
    for path in files:
        scenario = load_scenario(path)
        assert scenario.service in tripwire_ids
        for deployment in scenario.deployments:
            assert deployment in fleet.environments
        for fault in scenario.faults:
            assert fault.type in valid_faults
            assert fault.environment in fleet.environments
        for trip in scenario.expected.trips:
            assert trip.tripwire in tripwire_ids[scenario.service]
            assert trip.deployment in scenario.deployments
        for recovery in scenario.expected.recoveries:
            assert recovery.tripwire in tripwire_ids[scenario.service]
            assert recovery.deployment in scenario.deployments
        for guard in scenario.expected.mustNotTrip:
            assert guard.tripwire == "*" or guard.tripwire in tripwire_ids[scenario.service]
            assert guard.deployment in scenario.deployments
        if scenario.id in ("clean", "quiet-tenant"):
            assert scenario.expected.trips == []


def test_d1_t11_golden_paired():
    """D1-T11: Golden JSONL and expected files are paired and parse."""
    golden = ROOT / "fixtures" / "golden"
    files = sorted(golden.glob("*.jsonl"))
    assert len(files) >= 4
    for jsonl_path in files:
        expected_path = golden / f"{jsonl_path.stem}.expected.json"
        assert expected_path.exists(), f"missing {expected_path.name}"
        entries = read_jsonl(jsonl_path)
        assert len(entries) >= 1
        expected = json.loads(expected_path.read_text())
        assert "events" in expected
        for event in expected["events"]:
            for key in ("kind", "deployment", "tripwire", "atOffsetSeconds"):
                assert key in event


def test_d1_t12_mutants_present():
    """D1-T12: Mutants exist, carry _mutation and load via load_contract."""
    files = sorted((ROOT / "fixtures" / "mutants").glob("*.json"))
    assert len(files) >= 4
    for path in files:
        data = json.loads(path.read_text())
        assert "_mutation" in data
        assert isinstance(data["_mutation"], str) and data["_mutation"].strip()
        contract = load_contract(path)
        assert contract.service in {"alert-ingestion", "intel-sync"}


def test_d1_t13_chronicle_field_specs():
    """D1-T13: Chronicle specs exist with source URLs and §4.1 field lists."""
    # Literals copied from docs/mocks.md §4.1 (array brackets stripped).
    expected_fields = {
        "environments": [
            "name",
            "displayName",
            "description",
            "contact",
            "contactEmails",
            "contactPhone",
            "base64Image",
            "retentionDuration",
            "dynamicParameters",
            "system",
            "aliasesJson",
            "instanceUri",
            "dataAccessScopesJson",
            "weight",
            "id",
        ],
        "integrationInstances": [
            "name",
            "environment",
            "displayName",
            "parameters",
            "integrationIdentifier",
            "identifier",
            "description",
            "configured",
            "remote",
            "agent",
            "systemDefault",
        ],
        "connectorInstances": [
            "name",
            "agent",
            "environment",
            "displayName",
            "description",
            "intervalSeconds",
            "documentationLink",
            "allowList",
            "productFieldName",
            "eventFieldName",
            "timeoutSeconds",
            "connectorDefinitionName",
            "integrationVersion",
            "version",
            "loggingEnabledUntilUnixMs",
            "statistics",
            "parameters",
            "connectorId",
            "identifier",
            "id",
            "updateAvailable",
            "custom",
            "enabled",
            "remote",
            "status",
            "integration",
        ],
        "connectorInstanceLogs": ["name", "time", "message", "connectorInstanceId", "logLevel"],
        "jobInstances": [
            "name",
            "createTime",
            "updateTime",
            "displayName",
            "intervalSeconds",
            "script",
            "author",
            "lastRunTime",
            "parameters",
            "job",
            "integration",
            "advancedConfig",
            "nextScheduledRunTime",
            "custom",
            "enabled",
            "description",
            "lastRunStatus",
            "agent",
            "advanced",
            "uniqueIdentifier",
            "documentationLink",
        ],
        "jobInstanceLogs": [
            "name",
            "startTime",
            "endTime",
            "message",
            "jobIdentifier",
            "integration",
            "jobInstanceId",
            "status",
        ],
    }
    for name, fields in expected_fields.items():
        path = ROOT / "specs" / "chronicle" / f"{name}.yaml"
        assert path.exists(), f"missing {name}.yaml"
        data = yaml.safe_load(path.read_text())
        assert "docs.cloud.google.com/chronicle/docs/reference/rest/" in data["source"]
        assert data["fields"] == fields


def test_d1_t14_logging_decision():
    """D1-T14: Logging resource decision is recorded."""
    path = ROOT / "docs" / "decisions" / "logging-resource.md"
    assert path.exists()
    text = path.read_text()
    assert "result: accepted" in text or "result: fallback" in text
    assert "k8s_container" in text or "generic_task" in text


def test_d1_t15_holdouts_marked():
    """D1-T15: Exactly the specified scenarios are holdouts."""
    expected_holdouts = {
        ("alert-ingestion", "flap-mild"),
        ("alert-ingestion", "recovery"),
        ("intel-sync", "recovery"),
    }
    files = sorted((ROOT / "fixtures" / "scenarios").rglob("*.yaml"))
    assert len(files) >= 1
    seen: set[tuple[str, str]] = set()
    per_service: dict[str, list[Any]] = {}
    for path in files:
        scenario = load_scenario(path)
        key = (scenario.service, scenario.id)
        seen.add(key)
        per_service.setdefault(scenario.service, []).append(scenario)
        if key in expected_holdouts:
            assert scenario.holdout is True
        else:
            assert scenario.holdout is False
    assert expected_holdouts <= seen
    for scenarios in per_service.values():
        assert any(s.holdout for s in scenarios)
        assert any((not s.holdout) and len(s.faults) > 0 for s in scenarios)
