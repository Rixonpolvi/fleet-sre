"""Model gateway: per-role config over a Transport (Responses API)."""

import copy
import inspect
import time
from typing import Any, Callable, Protocol

from pydantic import BaseModel


class TransportError(Exception):
    """Model transport failure carrying the HTTP status."""

    def __init__(
        self, status: int, message: str = "", *, timeout: bool = False
    ) -> None:
        self.status = status
        self.timeout = timeout
        super().__init__(message or f"Transport error {status}")


class Transport(Protocol):
    def create(
        self, request: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]: ...


class RoleConfig(BaseModel):
    model: str
    reasoning_effort: str
    timeout_s: float = 120
    retry_timeouts: bool = True


class GatewayConfig(BaseModel):
    roles: dict[str, RoleConfig]
    base_url: str
    max_retries: int = 5
    prices: dict[str, dict[str, float]] = {}


class ModelResponse(BaseModel):
    output: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}


class ScriptedTransport:
    """Replay canned responses; {"_error": status} raises TransportError.

    An item may also carry "_timeout": true so the raised TransportError has
    timeout=True (d6b-pins 4). Timeouts passed by the gateway are recorded
    in .timeouts, alongside .requests.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.timeouts: list[float] = []

    def create(
        self, request: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(request))
        self.timeouts.append(timeout)
        response = self._responses[len(self.requests) - 1]
        if "_error" in response:
            raise TransportError(
                int(response["_error"]),
                timeout=bool(response.get("_timeout", False)),
            )
        return copy.deepcopy(response)


class OpenAITransport:
    """Live Responses API via the OpenAI SDK (spec.md §8)."""

    def __init__(
        self, *, api_key: str | None = None, base_url: str = ""
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(
            api_key=api_key, base_url=base_url or None, max_retries=0,
            timeout=120,
        )

    def create(
        self, request: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        try:
            response = self._client.responses.create(
                timeout=timeout, **request
            )
        except Exception as exc:
            from openai import APITimeoutError

            if isinstance(exc, APITimeoutError):
                raise TransportError(
                    504, str(exc) or "request timed out", timeout=True
                ) from exc
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


def _accepts_timeout(transport: Any) -> bool:
    """Whether transport.create takes a timeout keyword (d6b-pins 4).

    Every Transport in fleetsre/ and scripts/ does. Doubles written
    against the D5 interface take only the request; the gateway stays
    compatible with those instead of breaking their tests.
    """
    create: Any = getattr(transport, "create", None)
    if not callable(create):
        return False
    try:
        params = inspect.signature(create).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in params or any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )


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

    def _create(
        self, request: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        if _accepts_timeout(self.transport):
            return self.transport.create(request, timeout=timeout)
        return self.transport.create(request)

    def respond(
        self,
        role: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        text_format: dict[str, Any] | None = None,
    ) -> ModelResponse:
        role_config = self.config.roles[role]
        if tools is not None and text_format is not None:
            raise ValueError("pass tools or text_format, not both")
        request: dict[str, Any] = {
            "model": role_config.model,
            "input": input_items,
            "store": False,
        }
        if text_format is not None:
            request["text"] = {"format": text_format}
        else:
            # Key order matches the D5 request byte for byte.
            request["parallel_tool_calls"] = False
        request["include"] = ["reasoning.encrypted_content"]
        request["reasoning"] = {"effort": role_config.reasoning_effort}
        if tools is not None:
            request["tools"] = tools
        attempt = 0
        while True:
            try:
                response = self._create(request, role_config.timeout_s)
            except TransportError as exc:
                if exc.timeout and not role_config.retry_timeouts:
                    raise
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
