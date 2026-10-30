"""Ports: LogSink and the in-memory implementation."""

from typing import Protocol

from fleetsre.logmodel import LogEntry


class LogSink(Protocol):
    def write(self, entry: LogEntry) -> None: ...


class MemorySink(LogSink):
    entries: list[LogEntry]

    def __init__(self) -> None:
        self.entries = []

    def write(self, entry: LogEntry) -> None:
        self.entries.append(entry)
