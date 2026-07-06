from __future__ import annotations

from types import SimpleNamespace

from core.core.external_ai_bridge import BridgeExecResult, ExternalAIBridge
from core.core.models import Complexity, Task, TaskContext, TaskInput, TaskType


def _task() -> Task:
    task = Task(TaskType.CODE, TaskInput("Implement feature"), TaskContext("demo", ".", "main"))
    task.complexity = Complexity.MEDIUM
    task.session_id = "sess-bridge"
    return task


class _Response:
    def __init__(self, status_code: int, payload: dict[str, object], text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)
        self.content = b"{}"

    def json(self):
        return self._payload


def test_bridge_uses_direct_api_generation(monkeypatch):
    bridge = ExternalAIBridge()
    bridge.proxy_url = ""
    bridge.api_base_url = "https://example.test/v1beta/openai"
    bridge.chat_completions_endpoint = "https://example.test/v1beta/openai/chat/completions"
    bridge.api_key = "token"
    bridge.router = SimpleNamespace(build_plan=lambda task, prompt: SimpleNamespace(models=["antigravity-flash-lite", "antigravity-flash"]))
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(("POST", url, json or {}))
        assert headers == {"Content-Type": "application/json", "api-key": "token", "Authorization": "Bearer token"}
        assert url == "https://example.test/v1beta/openai/chat/completions"
        assert json["model"] == "gemini-2.5-flash-lite"
        return _Response(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("core.core.external_ai_bridge.httpx.post", fake_post)

    result = bridge.run_antigravity_cli(_task(), "prompt", timeout_sec=30)

    assert result.ok is True
    assert result.output == "ok"
    assert calls == [("POST", "https://example.test/v1beta/openai/chat/completions", {"model": "gemini-2.5-flash-lite", "messages": [{"role": "user", "content": "prompt"}], "max_completion_tokens": 1200, "temperature": 0.2, "stream": False})]


def test_bridge_uses_http_proxy_prompt_flow(monkeypatch):
    bridge = ExternalAIBridge()
    bridge.proxy_url = "http://proxy.test"
    bridge.api_base_url = "https://example.test/v1beta"
    bridge.api_key = "token"
    bridge.router = SimpleNamespace(build_plan=lambda task, prompt: SimpleNamespace(models=["antigravity-flash-lite"]))
    calls: list[tuple[str, str]] = []

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        calls.append((method, url))
        return _Response(200, {"ok": True, "stdout": "proxied ok"})

    monkeypatch.setattr("core.core.external_ai_bridge.httpx.request", fake_request)

    result = bridge.run_antigravity_cli(_task(), "prompt", timeout_sec=30)

    assert result.ok is True
    assert result.output == "proxied ok"
    assert calls == [("POST", "http://proxy.test/prompt")]


def test_bridge_treats_auth_prompt_output_as_failure(monkeypatch):
    bridge = ExternalAIBridge()
    bridge.proxy_url = ""
    bridge.api_base_url = "https://example.test/v1beta/openai"
    bridge.chat_completions_endpoint = "https://example.test/v1beta/openai/chat/completions"
    bridge.api_key = "token"
    bridge.router = SimpleNamespace(build_plan=lambda task, prompt: SimpleNamespace(models=["antigravity-flash-lite"]))

    def fake_post(url, headers=None, json=None, timeout=None):
        return _Response(200, {"choices": [{"message": {"content": "Authentication required. Error: authentication timed out."}}]})

    monkeypatch.setattr("core.core.external_ai_bridge.httpx.post", fake_post)

    result = bridge.run_antigravity_cli(_task(), "prompt", timeout_sec=30)

    assert result.ok is False
    assert result.error_type == "auth_fail"


def test_resolve_antigravity_cli_command_is_disabled():
    assert ExternalAIBridge.resolve_antigravity_cli_command() is None


def test_antigravity_runtime_env_preserves_keys_and_can_strip_them(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "token-a")
    monkeypatch.setenv("GEMINI_API_KEY", "token-b")
    monkeypatch.setenv("GOOGLE_API_KEY", "token-c")

    env = ExternalAIBridge._antigravity_runtime_env()
    assert env["ANTIGRAVITY_API_KEY"] == "token-a"
    assert env["GEMINI_API_KEY"] == "token-b"
    assert env["GOOGLE_API_KEY"] == "token-c"

    monkeypatch.setenv("AI_BRIDGE_ANTIGRAVITY_PREFER_OAUTH", "true")
    stripped = ExternalAIBridge._antigravity_runtime_env("agy")
    assert "ANTIGRAVITY_API_KEY" not in stripped
    assert "GEMINI_API_KEY" not in stripped
    assert "GOOGLE_API_KEY" not in stripped


def test_classify_error_covers_common_api_failures():
    assert ExternalAIBridge.classify_error("connection timed out") == "tcp_timeout"
    assert ExternalAIBridge.classify_error("gateway timeout 504") == "api_timeout"
    assert ExternalAIBridge.classify_error("resource_exhausted 429") == "quota_exhaustion"
    assert ExternalAIBridge.classify_error("invalid api key") == "auth_fail"


def test_bridge_falls_back_to_mimo_when_antigravity_upstream_is_unavailable(monkeypatch):
    bridge = ExternalAIBridge()
    bridge.proxy_url = ""
    bridge.chat_completions_endpoint = "https://example.test/v1beta/openai/chat/completions"
    bridge.api_key = "token"
    bridge.router = SimpleNamespace(build_plan=lambda task, prompt: SimpleNamespace(models=["antigravity-flash-lite"]))

    def fake_post(url, headers=None, json=None, timeout=None):
        return _Response(503, {}, text='[{"error":{"code":503,"message":"high demand","status":"UNAVAILABLE"}}]')

    monkeypatch.setattr("core.core.external_ai_bridge.httpx.post", fake_post)
    monkeypatch.setattr("core.core.external_ai_bridge.invoke_mimo_native", lambda model, prompt, timeout_sec=45.0, max_completion_tokens=1200, temperature=0.2: ({"choices": [{"message": {"content": "mimo ok"}}]}, None, 200))
    monkeypatch.setattr("core.core.external_ai_bridge.extract_mimo_response_text", lambda payload: "mimo ok")

    result = bridge.run_antigravity_cli(_task(), "prompt", timeout_sec=30)

    assert result.ok is True
    assert result.provider == "mimo"
    assert result.output == "mimo ok"
