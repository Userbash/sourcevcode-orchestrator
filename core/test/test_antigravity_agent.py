from __future__ import annotations

from core.agents.antigravity_agent import AntigravityDirectAgent
from core.core.models import Task, TaskContext, TaskInput, TaskStatus, TaskType


def _task() -> Task:
    return Task(
        TaskType.CODE,
        TaskInput("Implement from description"),
        TaskContext("demo", ".", "main"),
    )


def test_antigravity_agent_uses_task_description_prompt(monkeypatch):
    captured = {}

    def fake_invoke(model_name: str, prompt: str):
        captured["model_name"] = model_name
        captured["prompt"] = prompt
        return {"choices": [{"message": {"content": "ok"}}]}, "", 200

    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "test-key")
    monkeypatch.setattr("core.agents.antigravity_agent.invoke_antigravity_native", fake_invoke)
    agent = AntigravityDirectAgent("antigravity-1")

    result = agent.run(_task())

    assert result.status == TaskStatus.DONE
    assert captured["prompt"] == "Implement from description"
    assert captured["model_name"] == "gemini-2.5-flash-lite"


def test_antigravity_agent_returns_failed_when_not_configured(monkeypatch):
    monkeypatch.delenv("ANTIGRAVITY_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    agent = AntigravityDirectAgent("antigravity-1")

    result = agent.run(_task())

    assert result.status == TaskStatus.FAILED
    assert "ANTIGRAVITY_API_KEY" in " ".join(result.errors)
