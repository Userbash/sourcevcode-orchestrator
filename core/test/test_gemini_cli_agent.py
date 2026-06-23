from __future__ import annotations

from types import SimpleNamespace

from core.agents.gemini_cli_agent import GeminiCLIAgent
from core.core.external_ai_bridge import BridgeExecResult, ExternalAIBridge
from core.core.models import Task, TaskContext, TaskInput, TaskStatus, TaskType
from core.core.security import SecurityManager, SecurityPolicy


def _task() -> Task:
    return Task(
        TaskType.CODE,
        TaskInput("Implement feature"),
        TaskContext("demo", ".", "main"),
    )


def _agent() -> GeminiCLIAgent:
    policy = SecurityPolicy(allow_shell=True, shell_allowlist=["agy -p", "antigravity -p"])
    return GeminiCLIAgent("gemini-cli-1", SecurityManager(policy))


def test_gemini_cli_success_uses_direct_api_bridge(monkeypatch):
    agent = _agent()

    monkeypatch.setattr(
        ExternalAIBridge,
        "run_antigravity",
        lambda self, task, prompt, timeout_sec=120: BridgeExecResult(True, "ok", "", "antigravity", "antigravity-pro", 1, error_type="none"),
    )

    result = agent.run(_task())

    assert result.status == TaskStatus.DONE
    assert result.output["summary"] == "ok"


def test_gemini_cli_timeout_returns_failed(monkeypatch):
    agent = _agent()

    monkeypatch.setattr(
        ExternalAIBridge,
        "run_antigravity",
        lambda self, task, prompt, timeout_sec=120: BridgeExecResult(False, "", "request timeout", "antigravity", "antigravity-pro", 1, error_type="api_timeout"),
    )

    result = agent.run(_task())

    assert result.status == TaskStatus.FAILED
    assert "timed out" in result.output["summary"].lower()
    assert agent.active_tasks == 0
