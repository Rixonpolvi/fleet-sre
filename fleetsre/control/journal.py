"""Append-only executor journal (d6a-pins 4). Stub: behavior raises."""

from pathlib import Path


class Journal:
    def __init__(self, path: str | Path | None = None) -> None:
        raise NotImplementedError

    def append(self, record: dict) -> None:
        raise NotImplementedError

    def records(self) -> list[dict]:
        raise NotImplementedError
