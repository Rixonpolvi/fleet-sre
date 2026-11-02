"""OpenAITransport builds a 120s-timeout client; timeouts retry as 504."""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APITimeoutError

from fleetsre.sre.gateway import (
    GatewayConfig,
    ModelGateway,
    OpenAITransport,
    RoleConfig,
    TransportError,
)


def _timeout_error() -> APITimeoutError:
    return APITimeoutError(
        request=httpx.Request("POST", "https://api.test/v1/responses")
    )


def test_client_uses_timeout_120_and_no_retries(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    OpenAITransport(api_key="key", base_url="https://api.test/v1")
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 120


def test_timeout_surfaces_as_retryable_504() -> None:
    transport = OpenAITransport(api_key="key", base_url="https://x")
    responses = SimpleNamespace(
        create=lambda **kw: (_ for _ in ()).throw(_timeout_error())
    )
    transport._client = SimpleNamespace(responses=responses)
    with pytest.raises(TransportError) as excinfo:
        transport.create({"model": "m", "input": []}, timeout=120)
    assert excinfo.value.status == 504
    assert excinfo.value.timeout is True


def test_gateway_backs_off_and_retries_timeout() -> None:
    calls = {"count": 0}

    def flaky_create(**kwargs: Any) -> Any:
        calls["count"] += 1
        if calls["count"] <= 2:
            raise _timeout_error()
        return SimpleNamespace(
            output=[{"type": "reasoning", "summary": []}],
            usage={"input_tokens": 10, "output_tokens": 5},
        )

    transport = OpenAITransport(api_key="key", base_url="https://x")
    transport._client = SimpleNamespace(
        responses=SimpleNamespace(create=flaky_create)
    )
    slept: list[float] = []
    gateway = ModelGateway(
        GatewayConfig(
            roles={"investigate": RoleConfig(
                model="m", reasoning_effort="low"
            )},
            base_url="https://x",
            max_retries=5,
        ),
        transport,
        sleep=slept.append,
    )
    response = gateway.respond(
        "investigate", [{"role": "user", "content": "hi"}], None
    )
    assert slept == [1.0, 2.0]
    assert response.output == [{"type": "reasoning", "summary": []}]
    assert response.usage == {"input_tokens": 10, "output_tokens": 5}
