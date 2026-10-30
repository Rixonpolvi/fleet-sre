"""JSONL sink adapter (D3 stub)."""

from pathlib import Path

from fleetsre.logmodel import LogEntry
from fleetsre.ports import LogSink


class JsonlSink(LogSink):
    def __init__(self, path: str | Path) -> None:
        raise NotImplementedError

    def write(self, entry: LogEntry) -> None:
        raise NotImplementedError
