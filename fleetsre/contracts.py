"""D1 contracts: service contract loader (spec.md §5)."""

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import BaseModel, ConfigDict, ValidationError


class ContractError(Exception):
    """Loader failure with a code from deliverables.md §D1."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class Parameter(BaseModel):
    type: str
    required: bool
    default: Any = None
    description: str | None = None


class LogsConfig(BaseModel):
    container_name: str
    match: list[str]
    deploymentBy: str


class Component(BaseModel):
    id: str
    kind: str
    logs: LogsConfig
    description: str | None = None


class Tripwire(BaseModel):
    id: str
    component: str
    type: str
    spec: dict[str, Any]
    description: str | None = None


class ChangePoint(BaseModel):
    durationSeconds: int
    overrides: dict[str, dict[str, Any]]


class Provenance(BaseModel):
    authoredBy: str
    scorecard: str | None = None
    approvedBy: str | None = None


class Contract(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: str
    version: str
    description: str | None = None
    parameters: dict[str, Parameter]
    components: list[Component]
    tripwires: list[Tripwire]
    changePoint: ChangePoint
    provenance: Provenance


_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "contracts" / "schema.json"

_PARAM_REF_RE = re.compile(r"\$\{([A-Za-z][A-Za-z0-9]*)\}")

_schema_cache: dict[str, Any] | None = None
_spec_fields_cache: dict[str, set[str]] | None = None


def _load_schema() -> dict[str, Any]:
    global _schema_cache
    if _schema_cache is None:
        with open(_SCHEMA_PATH, encoding="utf-8") as fh:
            loaded: dict[str, Any] = json.load(fh)
        _schema_cache = loaded
    return _schema_cache


def _spec_fields_by_type() -> dict[str, set[str]]:
    """Allowed spec keys per tripwire type, from the schema's spec properties."""
    global _spec_fields_cache
    if _spec_fields_cache is None:
        schema = _load_schema()
        fields: dict[str, set[str]] = {}
        defs = schema["$defs"]
        for name in ("cadence", "synthetic", "schemaTripwire", "errorSignature"):
            tripwire_type = defs[name]["properties"]["type"]["const"]
            spec_props: dict[str, Any] = defs[name]["properties"]["spec"]["properties"]
            fields[tripwire_type] = set(spec_props)
        _spec_fields_cache = fields
    return _spec_fields_cache


def _find_param_refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return _PARAM_REF_RE.findall(value)
    if isinstance(value, dict):
        refs: list[str] = []
        for item in value.values():
            refs.extend(_find_param_refs(item))
        return refs
    if isinstance(value, list):
        refs = []
        for item in value:
            refs.extend(_find_param_refs(item))
        return refs
    return []


def parse_contract(data: dict[str, Any]) -> Contract:
    """Validate a contract dict: schema first, then the referential rules in
    the ContractError codes table order (deliverables.md §D1).

    Raises ContractError(code) on the first failure. Top-level keys starting
    with "_" are allowed by the schema and ignored by the loader.
    """
    raw = data
    schema = _load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    try:
        validator.validate(raw)
    except jsonschema.ValidationError as exc:
        raise ContractError("schema_invalid", str(exc.message)) from exc

    data = {k: v for k, v in raw.items() if not k.startswith("_")}
    try:
        contract = Contract.model_validate(data)
    except ValidationError as exc:
        raise ContractError("schema_invalid", str(exc)) from exc

    component_ids = {c.id for c in contract.components}
    for tripwire in contract.tripwires:
        if tripwire.component not in component_ids:
            raise ContractError(
                "unknown_component",
                f"tripwire {tripwire.id!r} refers to unknown component {tripwire.component!r}",
            )

    tripwire_ids = [t.id for t in contract.tripwires]
    if len(set(tripwire_ids)) != len(tripwire_ids):
        seen: set[str] = set()
        duplicate = ""
        for tid in tripwire_ids:
            if tid in seen:
                duplicate = tid
                break
            seen.add(tid)
        raise ContractError("duplicate_tripwire", f"duplicate tripwire id {duplicate!r}")

    for tripwire in contract.tripwires:
        for ref in _find_param_refs(tripwire.spec):
            if ref not in contract.parameters:
                raise ContractError(
                    "dangling_parameter",
                    f"tripwire {tripwire.id!r} refers to unknown parameter {ref!r}",
                )

    by_id = {t.id: t for t in contract.tripwires}
    for key in contract.changePoint.overrides:
        if key not in by_id:
            raise ContractError("unknown_override", f"override for unknown tripwire {key!r}")

    spec_fields = _spec_fields_by_type()
    for key, override in contract.changePoint.overrides.items():
        allowed = spec_fields.get(by_id[key].type)
        if allowed is None:
            continue
        for field in override:
            if field not in allowed:
                raise ContractError(
                    "unknown_override_field",
                    f"override for tripwire {key!r} sets unknown field {field!r}",
                )

    for tripwire in contract.tripwires:
        if tripwire.type != "errorSignature":
            continue
        effective = dict(tripwire.spec)
        effective.update(contract.changePoint.overrides.get(tripwire.id, {}))
        threshold = effective.get("threshold")
        window = effective.get("windowRuns")
        if (
            isinstance(threshold, int)
            and not isinstance(threshold, bool)
            and isinstance(window, int)
            and not isinstance(window, bool)
            and threshold > window
        ):
            raise ContractError(
                "threshold_exceeds_window",
                f"tripwire {tripwire.id!r} has threshold {threshold} > windowRuns {window}",
            )

    for component in contract.components:
        container = component.logs.container_name
        if component.kind in ("connectorInstance", "jobInstance") and container != "python":
            raise ContractError(
                "container_kind_mismatch",
                f"component {component.id!r} ({component.kind}) is on {container!r}, want 'python'",
            )
        if component.kind == "etlFlow" and container != "etl":
            raise ContractError(
                "container_kind_mismatch",
                f"component {component.id!r} (etlFlow) is on {container!r}, want 'etl'",
            )

    return contract


def load_contract(path: str | Path) -> Contract:
    """Read a contract file and validate it via parse_contract (d6b-pins 2)."""
    with open(path, encoding="utf-8") as fh:
        try:
            raw: dict[str, Any] = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ContractError("schema_invalid", f"invalid JSON: {exc}") from exc
    return parse_contract(raw)


class SchemaSpec(BaseModel):
    """D4: resolved schema tripwire spec (no ${param} strings). Data model only."""

    model_config = ConfigDict(extra="ignore")

    event: str | None = None
    field: str
    type: str
    enum: list[Any] | None = None
    minimum: int | float | None = None
    required: bool
    consecutive: int = 1
    recoverAfter: int | None = None
    where: dict[str, Any] | None = None


class ErrorSigSpec(BaseModel):
    """D4: resolved errorSignature spec (no ${param} strings). Data model only."""

    model_config = ConfigDict(extra="ignore")

    minSeverity: str
    windowRuns: int
    threshold: int
