"""JSONL sink adapter: append-only, one LogEntry per line."""

from pathlib import Path

from fleetsre.logmodel import LogEntry
from fleetsre.ports import LogSink


class JsonlSink(LogSink):
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def write(self, entry: LogEntry) -> None:
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(entry.model_dump_json() + "\n")
