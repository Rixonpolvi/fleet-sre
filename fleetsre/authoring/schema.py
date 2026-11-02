"""Authoring schema: flattened contract schema for text.format (d6b-pins 3)."""

import copy
import json
from pathlib import Path
from typing import Any


def _resolve(root: Any, ref: str) -> Any:
    node = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif (
            isinstance(node, list)
            and part.isdigit()
            and int(part) < len(node)
        ):
            node = node[int(part)]
        else:
            raise ValueError(f"unresolvable $ref: {ref}")
    return node


def _inline(node: Any, root: Any, stack: tuple[str, ...]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in stack:
                raise ValueError(f"recursive $ref: {ref}")
            inlined = _inline(
                copy.deepcopy(_resolve(root, ref)), root, stack + (ref,)
            )
            rest = {
                key: _inline(value, root, stack)
                for key, value in node.items()
                if key != "$ref"
            }
            if rest and isinstance(inlined, dict):
                merged = dict(inlined)
                merged.update(rest)
                return merged
            return inlined
        return {
            key: _inline(value, root, stack) for key, value in node.items()
        }
    if isinstance(node, list):
        return [_inline(value, root, stack) for value in node]
    return node


def flatten_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline every local $ref; drop $defs/definitions and meta keys.

    Raises ValueError on a recursive $ref (the API returns 400 on one;
    spec.md §8).
    """
    root = copy.deepcopy(schema)
    flat = _inline(root, root, ())
    if not isinstance(flat, dict):
        raise ValueError("schema root must be an object")
    for key in ("$defs", "definitions", "$schema", "$id", "$comment"):
        flat.pop(key, None)
    return flat


def _schema_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "contracts"
        / "schema.json"
    )


def authoring_schema() -> dict[str, Any]:
    """Flattened contract schema minus provenance and _-keys (d6b-pins 3)."""
    with open(_schema_path(), encoding="utf-8") as fh:
        schema: dict[str, Any] = json.load(fh)
    flat = flatten_schema(schema)
    properties = flat.get("properties")
    if isinstance(properties, dict):
        properties.pop("provenance", None)
    required = flat.get("required")
    if isinstance(required, list) and "provenance" in required:
        flat["required"] = [name for name in required if name != "provenance"]
    flat.pop("patternProperties", None)
    return flat
