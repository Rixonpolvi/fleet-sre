"""ToolSet: read tools (§6.2) plus working tools (§6.3) with gates inside."""

from typing import Any


class ToolSet:
    def __init__(
        self,
        log_query: Any,
        chronicle: Any,
        state: Any,
        registry: Any,
        *,
        clock: Any,
        verification: bool = True,
    ) -> None:
        raise NotImplementedError

    def specs(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def call(
        self,
        name: str,
        args: dict[str, Any],
        session: Any,
        *,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError
