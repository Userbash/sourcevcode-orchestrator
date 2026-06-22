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


def test_codex_agent_retries_after_claude_pool_error(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setattr(codex_agent_module, "OpenAI", _FakeOpenAIClient)

    agent = CodexAgent("codex-main")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "false")
    agent.set_identity(provider="openai", model_name="gpt-5.5")
    task = _task()
    task.assigned_model = "claude-sonnet-4-6"

    _FakeOpenAIClient.seen_models = []
    result = agent.run(task)

    assert result.status.value == "done"
    assert result.model_name == agent.model_name
    assert _FakeOpenAIClient.seen_models == ["claude-sonnet-4-6", agent.model_name]


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
