"""Chronicle HTTP client over httpx (dev auth header; ID tokens in D7)."""

from typing import Any

import httpx

from fleetsre.ports import ChronicleError


class ChronicleHttpClient:
    """ChronicleClient over HTTP with X-Fleetsre-Identity (mocks.md §4.2)."""

    def __init__(self, http: httpx.Client, identity: str) -> None:
        self._http = http
        self._identity = identity

    @property
    def identity(self) -> str:
        return self._identity

    def _headers(self) -> dict[str, str]:
        return {"X-Fleetsre-Identity": self._identity}

    def _raise(self, response: Any) -> None:
        try:
            parsed = response.json()
        except ValueError:
            parsed = {"message": response.text}
        body = parsed.get("error", parsed) if isinstance(parsed, dict) else {
            "message": str(parsed)
        }
        if not isinstance(body, dict):
            body = {"message": str(body)}
        raise ChronicleError(response.status_code, body)

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = self._http.get(url, params=params, headers=self._headers())
        if response.status_code >= 400:
            self._raise(response)
        return response.json()

    def _post(self, url: str, body: Any) -> Any:
        response = self._http.post(url, json=body, headers=self._headers())
        if response.status_code >= 400:
            self._raise(response)
        return response.json()

    def get(self, name: str) -> dict[str, Any]:
        return self._get(f"/v1/{name}")

    def list_connector_logs(
        self, name: str, page_size: int
    ) -> dict[str, Any]:
        return self._get(f"/v1/{name}/logs", {"pageSize": page_size})

    def list_job_logs(self, name: str, page_size: int) -> dict[str, Any]:
        return self._get(f"/v1/{name}/logs", {"pageSize": page_size})

    def execute_test(self, name: str) -> dict[str, Any]:
        return self._post(f"/v1/{name}:executeTest", {})

    def patch(
        self, name: str, body: dict[str, Any], update_mask: list[str]
    ) -> dict[str, Any]:
        response = self._http.patch(
            f"/v1/{name}",
            params={"updateMask": ",".join(update_mask)},
            json=body,
            headers=self._headers(),
        )
        if response.status_code >= 400:
            self._raise(response)
        return response.json()

    def run_on_demand(
        self, name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._post(f"/v1/{name}:runOnDemand", body)

    def set_logs_collection(
        self, name: str, enabled: bool
    ) -> dict[str, Any]:
        return self._post(f"/v1/{name}:setLogsCollection", {"enabled": enabled})
