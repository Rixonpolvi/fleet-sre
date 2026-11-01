"""Append-only executor journal (d6a-pins 4)."""

import copy
import json
from pathlib import Path
from typing import Any


class Journal:
    """Append-only record store, optionally backed by a JSONL file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._records: list[dict[str, Any]] = []
        if self._path is not None and self._path.is_file():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    loaded = json.loads(line)
                    if isinstance(loaded, dict):
                        self._records.append(loaded)

    def append(self, record: dict[str, Any]) -> None:
        stored = copy.deepcopy(record)
        self._records.append(stored)
        if self._path is not None:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(stored) + "\n")
                handle.flush()

    def records(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._records)
