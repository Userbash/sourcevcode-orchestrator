from __future__ import annotations

import httpx

from core.core.local_model_runtime import (
    LocalModelClient,
    LocalModelGenerationRequest,
    LocalModelRuntimeConfig,
    LocalModelRetryPolicy,
)


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object] | None, float]] = []

    def request(self, method: str, url: str, json=None, timeout: float = 0.0):
        self.calls.append((method, url, json, timeout))
        action = self._responses.pop(0)
        if isinstance(action, Exception):
            raise action
        return action(method, url, json, timeout) if callable(action) else action

    def close(self):
        return None



def _response(status_code: int, payload: dict[str, object]):
    request = httpx.Request("POST" if status_code != 200 or "response" in payload else "GET", "http://example.test")
    return httpx.Response(status_code, request=request, json=payload)



def test_local_model_runtime_health_fails_over_to_secondary_endpoint_and_remembers_it():
    client = _FakeClient([
        httpx.ConnectError("primary down"),
        httpx.ConnectError("primary still down"),
        _response(200, {"models": [{"name": "qwen2.5:latest"}]}),
    ])
    runtime = LocalModelClient(
        LocalModelRuntimeConfig(
            endpoint="http://host.containers.internal:11434",
            fallback_endpoints=("http://127.0.0.1:11434",),
            model_name="qwen2.5:32b-instruct-q4_k_m",
        ),
        client=client,
    )

    health = runtime.health()

    assert health.ok is True
    assert health.ready is True
    assert health.endpoint == "http://127.0.0.1:11434"
    assert runtime.endpoint == "http://127.0.0.1:11434"
    assert client.calls[0][1] == "http://host.containers.internal:11434/api/tags"
    assert client.calls[1][1] == "http://host.containers.internal:11434/api/tags"
    assert client.calls[2][1] == "http://127.0.0.1:11434/api/tags"



def test_local_model_runtime_health_is_degraded_when_service_is_up_but_model_missing():
    client = _FakeClient([
        _response(200, {"models": [{"name": "llama3:latest"}]}),
        _response(200, {"models": [{"name": "llama3:latest"}]}),
    ])
    runtime = LocalModelClient(
        LocalModelRuntimeConfig(endpoint="http://127.0.0.1:11434", model_name="qwen2.5:32b-instruct-q4_k_m"),
        client=client,
    )

    health = runtime.health()
    readiness = runtime.readiness()

    assert health.ok is True
    assert health.ready is False
    assert health.status == "degraded"
    assert readiness["ok"] is False
    assert readiness["service_reachable"] is True
    assert readiness["model_present"] is False



def test_local_model_runtime_generate_retries_retryable_status_then_returns_structured_result():
    retry_request = httpx.Request("POST", "http://127.0.0.1:11434/api/generate")
    retry_response = httpx.Response(503, request=retry_request, json={"error": "warming up"})
    client = _FakeClient([
        httpx.HTTPStatusError("retry me", request=retry_request, response=retry_response),
        _response(
            200,
            {
                "response": '{"summary":"structured"}',
                "prompt_eval_count": 5,
                "eval_count": 3,
                "total_duration": 1_500_000_000,
                "load_duration": 200_000_000,
                "prompt_eval_duration": 300_000_000,
                "eval_duration": 400_000_000,
                "done": True,
                "done_reason": "stop",
            },
        ),
    ])
    runtime = LocalModelClient(
        LocalModelRuntimeConfig(
            endpoint="http://local-model.test:11434",
            retry_policy=LocalModelRetryPolicy(max_attempts=2, backoff_base_sec=0.0),
        ),
        client=client,
    )

    result = runtime.generate(LocalModelGenerationRequest(prompt="summarize changes"))

    assert result.text == '{"summary":"structured"}'
    assert result.endpoint == "http://local-model.test:11434"
    assert result.metrics.attempts == 2
    assert result.metrics.latency_ms == 1500.0
    assert result.metrics.prompt_eval_count == 5
    assert result.metrics.eval_count == 3
    assert result.metrics.done_reason == "stop"



def test_local_model_runtime_generate_uses_requested_format_and_options_payload():
    captured: dict[str, object] = {}

    def responder(method: str, url: str, json: dict[str, object] | None, timeout: float):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _response(200, {"response": "{\"status\":\"ok\"}"})

    client = _FakeClient([responder])
    runtime = LocalModelClient(LocalModelRuntimeConfig(endpoint="http://127.0.0.1:11434"), client=client)

    result = runtime.generate(
        LocalModelGenerationRequest(
            prompt="emit structured result",
            model_name="custom-local",
            system="kernel helper",
            options={"temperature": 0.1},
            timeout_sec=12.0,
            format={"type": "object"},
        )
    )

    assert result.text == '{"status":"ok"}'
    assert captured["json"] == {
        "model": "custom-local",
        "prompt": "emit structured result",
        "system": "kernel helper",
        "stream": False,
        "options": {"temperature": 0.1},
        "format": {"type": "object"},
    }
    assert captured["timeout"] == 12.0



def test_local_model_runtime_generate_raises_after_transport_failures_exhaust_retries():
    client = _FakeClient([
        httpx.ReadTimeout("timeout-1"),
        httpx.ReadTimeout("timeout-2"),
    ])
    runtime = LocalModelClient(
        LocalModelRuntimeConfig(
            endpoint="http://local-model.test:11434",
            retry_policy=LocalModelRetryPolicy(max_attempts=2, backoff_base_sec=0.0),
        ),
        client=client,
    )

    try:
        runtime.generate(LocalModelGenerationRequest(prompt="retry until failure"))
    except RuntimeError as exc:
        assert "timeout-2" in str(exc)
    else:
        raise AssertionError("expected RuntimeError after retries are exhausted")
