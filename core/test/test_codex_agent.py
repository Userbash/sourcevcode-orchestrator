from __future__ import annotations

from types import SimpleNamespace

from core.agents import codex_agent as codex_agent_module
from core.agents.codex_agent import CodexAgent
from core.core.models import Task, TaskContext, TaskInput, TaskType


def _task() -> Task:
    return Task(
        TaskType.CODE,
        TaskInput("implement async orchestrator retry guard", files=[]),
        TaskContext("repo", ".", "main"),
    )


class _FakeOpenAIClient:
    seen_models: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model: str, messages, temperature: float):
        self.seen_models.append(model)
        if model == "claude-sonnet-4-6":
            raise Exception("Claude pool has no eligible resources")
        if model == "gpt-4o-transcribe":
            raise Exception("The 'gpt-4o-transcribe' model is not supported when using Codex with a ChatGPT account.")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=f"ok from {model}"))],
            usage=SimpleNamespace(total_tokens=42),
        )


class _FakeResponsesOpenAIClient:
    chat_calls: list[str] = []
    responses_calls: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat_create))
        self.responses = SimpleNamespace(create=self._responses_create)

    def _chat_create(self, *, model: str, messages, temperature: float):
        self.chat_calls.append(model)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=f"chat from {model}"))],
            usage=SimpleNamespace(total_tokens=21),
        )

    def _responses_create(self, *, model: str, input, temperature: float, **kwargs):
        self.responses_calls.append(model)
        return {
            "id": "resp_1",
            "output_text": f"responses from {model}",
            "usage": {"total_tokens": 64},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": f"responses from {model}"}]}],
        }


class _FakeFallbackResponsesOpenAIClient(_FakeResponsesOpenAIClient):
    def _responses_create(self, *, model: str, input, temperature: float, **kwargs):
        self.responses_calls.append(model)
        raise Exception("Responses endpoint unsupported: 404 not found")


class _FakeEmptyResponsesOpenAIClient(_FakeResponsesOpenAIClient):
    def _responses_create(self, *, model: str, input, temperature: float, **kwargs):
        self.responses_calls.append(model)
        return {"id": "resp_empty", "output": []}


def test_codex_agent_skips_blocked_claude_pool_model(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setattr(codex_agent_module, "OpenAI", _FakeOpenAIClient)
    runtime_inventory = tmp_path / "openai_runtime_inventory.json"
    runtime_inventory.write_text(
        '{"fully_routable_models": ["gpt-5.5"], "validated_models": [{"model": "claude-sonnet-4-6", "chat_completions": {"ok": false, "error": "Claude pool has no eligible resources"}, "responses": {"ok": false, "error": "Claude pool has no eligible resources"}}, {"model": "gpt-5.5", "chat_completions": {"ok": true}, "responses": {"ok": true}}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(runtime_inventory))
    monkeypatch.setenv("AI_BRIDGE_OPENAI_REQUIRE_ROUTABLE_MODELS", "true")

    agent = CodexAgent("codex-main")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "false")
    agent.set_identity(provider="openai", model_name="gpt-5.5")
    task = _task()
    task.assigned_model = "claude-sonnet-4-6"

    _FakeOpenAIClient.seen_models = []
    result = agent.run(task)

    assert result.status.value == "done"
    assert result.model_name == agent.model_name
    assert _FakeOpenAIClient.seen_models == [agent.model_name]


def test_codex_agent_skips_transcribe_model_and_uses_chat_model(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setattr(codex_agent_module, "OpenAI", _FakeOpenAIClient)

    agent = CodexAgent("codex-main")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "false")
    agent.set_identity(provider="openai", model_name="gpt-5.5")
    task = _task()
    task.assigned_model = "gpt-4o-transcribe"

    _FakeOpenAIClient.seen_models = []
    result = agent.run(task)

    assert result.status.value == "done"
    assert result.model_name == agent.model_name
    assert _FakeOpenAIClient.seen_models == [agent.model_name]


def test_codex_agent_skips_runtime_ineligible_preferred_model(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setattr(codex_agent_module, "OpenAI", _FakeOpenAIClient)
    runtime_inventory = tmp_path / "openai_runtime_inventory.json"
    runtime_inventory.write_text(
        '{"fully_routable_models": ["gpt-5.5"], "validated_models": [{"model": "claude-sonnet-4-6", "chat_completions": {"ok": false, "error": "Claude pool has no eligible resources"}, "responses": {"ok": false, "error": "Claude pool has no eligible resources"}}, {"model": "gpt-5.5", "chat_completions": {"ok": true}, "responses": {"ok": true}}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(runtime_inventory))
    monkeypatch.setenv("AI_BRIDGE_OPENAI_REQUIRE_ROUTABLE_MODELS", "true")

    agent = CodexAgent("codex-main")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "true")
    agent.set_identity(provider="openai", model_name="gpt-5.5")
    task = _task()
    task.assigned_model = "claude-sonnet-4-6"

    _FakeOpenAIClient.seen_models = []
    result = agent.run(task)

    assert result.status.value == "done"
    assert result.model_name == "gpt-5.5"
    assert _FakeOpenAIClient.seen_models == ["gpt-5.5"]


def test_codex_agent_prefers_responses_runtime_when_available(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_USE_RESPONSES", "true")
    monkeypatch.setattr(codex_agent_module, "OpenAI", _FakeResponsesOpenAIClient)

    agent = CodexAgent("codex-main")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "false")
    agent.set_identity(provider="openai", model_name="gpt-5.5")

    _FakeResponsesOpenAIClient.chat_calls = []
    _FakeResponsesOpenAIClient.responses_calls = []
    result = agent.run(_task())

    assert result.status.value == "done"
    assert result.output.summary == "responses from gpt-5.5"
    assert _FakeResponsesOpenAIClient.responses_calls == ["gpt-5.5"]
    assert _FakeResponsesOpenAIClient.chat_calls == []


def test_codex_agent_falls_back_to_chat_when_responses_endpoint_is_unavailable(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_USE_RESPONSES", "true")
    monkeypatch.setattr(codex_agent_module, "OpenAI", _FakeFallbackResponsesOpenAIClient)

    agent = CodexAgent("codex-main")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "false")
    agent.set_identity(provider="openai", model_name="gpt-5.5")

    _FakeFallbackResponsesOpenAIClient.chat_calls = []
    _FakeFallbackResponsesOpenAIClient.responses_calls = []
    result = agent.run(_task())

    assert result.status.value == "done"
    assert result.output.summary == "chat from gpt-5.5"
    assert _FakeFallbackResponsesOpenAIClient.responses_calls == ["gpt-5.5"]
    assert _FakeFallbackResponsesOpenAIClient.chat_calls == ["gpt-5.5"]

def test_codex_agent_falls_back_to_chat_when_responses_returns_empty_payload(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_USE_RESPONSES", "true")
    monkeypatch.setattr(codex_agent_module, "OpenAI", _FakeEmptyResponsesOpenAIClient)

    agent = CodexAgent("codex-main")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "false")
    agent.set_identity(provider="openai", model_name="gpt-5.5")

    _FakeEmptyResponsesOpenAIClient.chat_calls = []
    _FakeEmptyResponsesOpenAIClient.responses_calls = []
    result = agent.run(_task())

    assert result.status.value == "done"
    assert result.output.summary == "chat from gpt-5.5"
    assert _FakeEmptyResponsesOpenAIClient.responses_calls == ["gpt-5.5"]
    assert _FakeEmptyResponsesOpenAIClient.chat_calls == ["gpt-5.5"]

