"""Unit tests for D1 loaders: rules pinned by the goal beyond the acceptance tests."""

import copy
import json
from pathlib import Path

import pytest

from fleetsre.contracts import ContractError, load_contract

ROOT = Path(__file__).resolve().parents[2]


def _alert_contract() -> dict:
    return json.loads((ROOT / "contracts" / "alert-ingestion.json").read_text())


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(data))
    return path


def test_override_field_allowed_by_schema_but_absent_from_spec(tmp_path):
    """unknown_override_field is checked against the schema's spec properties,
    not only the keys present in the tripwire's spec."""
    data = _alert_contract()
    # 'minimum' is a valid schema-tripwire spec property but priority-shape has no such key.
    assert "minimum" not in data["tripwires"][2]["spec"]
    data["changePoint"]["overrides"] = {"priority-shape": {"minimum": 0}}
    contract = load_contract(_write(tmp_path, data))
    assert contract.changePoint.overrides == {"priority-shape": {"minimum": 0}}


def test_override_field_rejected_when_not_in_schema(tmp_path):
    data = _alert_contract()
    data["changePoint"]["overrides"] = {"priority-shape": {"bogus": 1}}
    with pytest.raises(ContractError) as excinfo:
        load_contract(_write(tmp_path, data))
    assert excinfo.value.code == "unknown_override_field"


def test_first_failure_wins_in_table_order(tmp_path):
    """unknown_component precedes duplicate_tripwire in the codes table."""
    data = _alert_contract()
    broken = copy.deepcopy(data)
    broken["tripwires"][2]["component"] = "nope"
    broken["tripwires"][3]["id"] = "canary"
    with pytest.raises(ContractError) as excinfo:
        load_contract(_write(tmp_path, broken))
    assert excinfo.value.code == "unknown_component"


def test_underscore_keys_ignored(tmp_path):
    data = _alert_contract()
    data["_expectError"] = "whatever"
    data["_mutation"] = "whatever"
    contract = load_contract(_write(tmp_path, data))
    assert contract.service == "alert-ingestion"
