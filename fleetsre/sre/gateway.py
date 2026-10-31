"""Model gateway: per-role config over a Transport (Responses API)."""

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
    """D5 stub: replay canned responses; {"_error": status} raises."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class OpenAITransport:
    """D5 stub: live Responses API via the OpenAI SDK (not tested offline)."""

    def __init__(
        self, *, api_key: str | None = None, base_url: str = ""
    ) -> None:
        raise NotImplementedError

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class ModelGateway:
    def __init__(
        self,
        config: GatewayConfig,
        transport: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        raise NotImplementedError

    def respond(
        self,
        role: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ModelResponse:
        raise NotImplementedError
