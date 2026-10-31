"""Model gateway: per-role config over a Transport (Responses API)."""

import copy
import time
from typing import Any, Callable, Protocol

from pydantic import BaseModel


class TransportError(Exception):
    """Model transport failure carrying the HTTP status."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        super().__init__(message or f"Transport error {status}")


class Transport(Protocol):
    def create(self, request: dict[str, Any]) -> dict[str, Any]: ...


class RoleConfig(BaseModel):
    model: str
    reasoning_effort: str


class GatewayConfig(BaseModel):
    roles: dict[str, RoleConfig]
    base_url: str
    max_retries: int = 5
    prices: dict[str, dict[str, float]] = {}


class ModelResponse(BaseModel):
    output: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}


class ScriptedTransport:
    """Replay canned responses; {"_error": status} raises TransportError."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(request))
        response = self._responses[len(self.requests) - 1]
        if "_error" in response:
            raise TransportError(int(response["_error"]))
        return copy.deepcopy(response)


class OpenAITransport:
    """Live Responses API via the OpenAI SDK (spec.md §8)."""

    def __init__(
        self, *, api_key: str | None = None, base_url: str = ""
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(
            api_key=api_key, base_url=base_url or None, max_retries=0
        )

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.responses.create(**request)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status is None:
                status = getattr(
                    getattr(exc, "response", None), "status_code", None
                )
            raise TransportError(
                int(status) if status is not None else 500, str(exc)
            ) from exc
        output: list[dict[str, Any]] = []
        for item in getattr(response, "output", []) or []:
            if hasattr(item, "model_dump"):
                output.append(item.model_dump(exclude_none=True))
            elif isinstance(item, dict):
                output.append(
                    {k: v for k, v in item.items() if v is not None}
                )
            else:
                output.append({"value": str(item)})
        usage = getattr(response, "usage", None)
        if usage is not None and hasattr(usage, "model_dump"):
            usage_dict = usage.model_dump(exclude_none=True)
        elif isinstance(usage, dict):
            usage_dict = dict(usage)
        else:
            usage_dict = {}
        return {"output": output, "usage": usage_dict}


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


class ModelGateway:
    """Per-role gateway with backoff on 429/5xx (d5-pins.md pin 17)."""

    def __init__(
        self,
        config: GatewayConfig,
        transport: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.transport = transport
        self._sleep = sleep

    def respond(
        self,
        role: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ModelResponse:
        role_config = self.config.roles[role]
        request: dict[str, Any] = {
            "model": role_config.model,
            "input": input_items,
            "store": False,
            "parallel_tool_calls": False,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": role_config.reasoning_effort},
        }
        if tools is not None:
            request["tools"] = tools
        attempt = 0
        while True:
            try:
                response = self.transport.create(request)
            except TransportError as exc:
                if not _retryable(exc.status) or (
                    attempt >= self.config.max_retries
                ):
                    raise
                self._sleep(float(2**attempt))
                attempt += 1
                continue
            output = response.get("output", [])
            usage = response.get("usage", {})
            if not isinstance(output, list):
                output = []
            if not isinstance(usage, dict):
                usage = {}
            return ModelResponse(output=output, usage=usage)
