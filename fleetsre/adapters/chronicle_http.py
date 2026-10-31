"""Chronicle HTTP client over httpx (dev auth header; ID tokens in D7)."""

from typing import Any

import httpx


class ChronicleHttpClient:
    """D5 stub: ChronicleClient over HTTP with X-Fleetsre-Identity."""

    def __init__(self, http: httpx.Client, identity: str) -> None:
        raise NotImplementedError

    @property
    def identity(self) -> str:
        raise NotImplementedError

    def get(self, name: str) -> dict[str, Any]:
        raise NotImplementedError

    def list_connector_logs(
        self, name: str, page_size: int
    ) -> dict[str, Any]:
        raise NotImplementedError

    def list_job_logs(self, name: str, page_size: int) -> dict[str, Any]:
        raise NotImplementedError

    def execute_test(self, name: str) -> dict[str, Any]:
        raise NotImplementedError

    def patch(
        self, name: str, body: dict[str, Any], update_mask: list[str]
    ) -> dict[str, Any]:
        raise NotImplementedError

    def run_on_demand(
        self, name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError

    def set_logs_collection(
        self, name: str, enabled: bool
    ) -> dict[str, Any]:
        raise NotImplementedError
