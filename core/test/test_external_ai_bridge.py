from __future__ import annotations

import subprocess
from types import SimpleNamespace

from core.core.external_ai_bridge import ExternalAIBridge
from core.core.models import Complexity, Task, TaskContext, TaskInput, TaskType


def _task() -> Task:
    task = Task(TaskType.CODE, TaskInput("Implement feature"), TaskContext("demo", ".", "main"))
    task.complexity = Complexity.MEDIUM
    task.session_id = "sess-bridge"
    return task


def test_bridge_fallbacks_to_next_model_on_capacity_error(monkeypatch):
    bridge = ExternalAIBridge()
    calls: list[list[str]] = []
    monkeypatch.setattr(ExternalAIBridge, "resolve_antigravity_cli_command", staticmethod(lambda: ["agy"]))
    monkeypatch.setattr(bridge.router, "build_plan", lambda task, prompt: SimpleNamespace(models=["antigravity-flash-lite", "antigravity-flash"]))

    def fake_run(cmd, capture_output, text, timeout, env=None, cwd=None):
        calls.append(cmd)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="RESOURCE_EXHAUSTED MODEL_CAPACITY_EXHAUSTED")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = bridge.run_antigravity_cli(_task(), "prompt", timeout_sec=30)

    assert result.ok is True
    assert len(calls) >= 2
    assert result.output == "ok"


def test_classify_error_timeout_types():
    assert ExternalAIBridge.classify_error("connection timed out") == "tcp_timeout"
    assert ExternalAIBridge.classify_error("gateway timeout 504") == "api_timeout"
    assert ExternalAIBridge.classify_error("resource_exhausted 429") == "quota_exhaustion"
    assert ExternalAIBridge.classify_error("invalid api key") == "auth_fail"


def test_bridge_treats_auth_prompt_output_as_failure(monkeypatch):
    bridge = ExternalAIBridge()
    monkeypatch.setattr(ExternalAIBridge, "resolve_antigravity_cli_command", staticmethod(lambda: ["agy"]))

    def fake_run(cmd, capture_output, text, timeout, env=None, cwd=None):
        return SimpleNamespace(returncode=0, stdout="Authentication required. Error: authentication timed out.", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = bridge.run_antigravity_cli(_task(), "prompt", timeout_sec=30)

    assert result.ok is False
    assert result.error_type == "auth_fail"


def test_resolve_antigravity_cli_command_rejects_gemini_alias(monkeypatch):
    monkeypatch.setattr("core.core.external_ai_bridge.shutil.which", lambda name: "/tmp/gemini" if name == "gemini" else None)
    monkeypatch.setattr("core.core.external_ai_bridge.os.path.isfile", lambda path: path == "/tmp/gemini")
    monkeypatch.setattr("core.core.external_ai_bridge.os.access", lambda path, mode: path == "/tmp/gemini")

    assert ExternalAIBridge.resolve_antigravity_cli_command() is None


def test_antigravity_runtime_env_preserves_keys_when_oauth_not_forced(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "token-a")
    monkeypatch.setenv("GEMINI_API_KEY", "token-b")
    monkeypatch.setenv("GOOGLE_API_KEY", "token-c")

    env = ExternalAIBridge._antigravity_runtime_env()

    assert env["PATH"].startswith("/home/tester/.npm-packages/bin")
    assert "/home/tester/.local/bin" in env["PATH"]
    assert env["ANTIGRAVITY_API_KEY"] == "token-a"
    assert env["GEMINI_API_KEY"] == "token-b"
    assert env["GOOGLE_API_KEY"] == "token-c"


def test_antigravity_runtime_env_strips_keys_when_oauth_forced(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "token-a")
    monkeypatch.setenv("GEMINI_API_KEY", "token-b")
    monkeypatch.setenv("GOOGLE_API_KEY", "token-c")

    monkeypatch.setenv("AI_BRIDGE_ANTIGRAVITY_PREFER_OAUTH", "true")

    env = ExternalAIBridge._antigravity_runtime_env("agy")

    assert "ANTIGRAVITY_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env

def test_bridge_timeout_preserves_partial_capacity_error(monkeypatch):
    bridge = ExternalAIBridge()
    monkeypatch.setattr(ExternalAIBridge, "resolve_antigravity_cli_command", staticmethod(lambda: ["agy"]))

    def fake_run(cmd, capture_output, text, timeout, env=None, cwd=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout, stderr="429 RESOURCE_EXHAUSTED MODEL_CAPACITY_EXHAUSTED")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = bridge.run_antigravity_cli(_task(), "prompt", timeout_sec=30)

    assert result.ok is False
    assert "RESOURCE_EXHAUSTED" in result.error
    assert result.error_type == "quota_exhaustion"

