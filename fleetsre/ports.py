"""Ports: LogSink and MemorySink (D3 stub)."""

from typing import Protocol

from fleetsre.logmodel import LogEntry


class LogSink(Protocol):
    def write(self, entry: LogEntry) -> None: ...


class MemorySink(LogSink):
    entries: list[LogEntry]

    def __init__(self) -> None:
        raise NotImplementedError

    def write(self, entry: LogEntry) -> None:
        raise NotImplementedError
