from __future__ import annotations

from types import SimpleNamespace

from core.agents.antigravity_cli_agent import AntigravityCLIAgent
from core.core.external_ai_bridge import BridgeExecResult, ExternalAIBridge
from core.core.models import AgentStatus, Task, TaskContext, TaskInput, TaskStatus, TaskType
from core.core.security import SecurityManager, SecurityPolicy


def _task() -> Task:
    return Task(
        TaskType.CODE,
        TaskInput("Implement feature"),
        TaskContext("demo", ".", "main"),
    )


def _agent() -> AntigravityCLIAgent:
    policy = SecurityPolicy(allow_shell=True, shell_allowlist=["agy -p", "antigravity -p"])
    return AntigravityCLIAgent("antigravity-cli-1", SecurityManager(policy))


def test_antigravity_cli_agent_health_reflects_manager_degradation(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_ANTIGRAVITY_ENABLE_PROVIDER_FALLBACK", "false")
    monkeypatch.setattr(
        'core.agents.antigravity_cli_agent.AntigravityManager',
        lambda: SimpleNamespace(
            status=lambda: {
                'ready': False,
                'inventory_ok': True,
                'api_probe': {'status_code': 403, 'error': 'permission_denied'},
                'auth_probe': {'stderr': 'authentication required'},
                'models_probe': {'stderr': 'api inventory unavailable'},
            }
        ),
    )
    agent = AntigravityCLIAgent('antigravity-1')

    health = agent.health()

    assert health.status == AgentStatus.DEGRADED
    assert 'permission_denied' in str(health.last_error)


def test_antigravity_cli_agent_health_is_ready_when_mimo_fallback_is_ready(monkeypatch):
    monkeypatch.setattr(
        'core.agents.antigravity_cli_agent.AntigravityManager',
        lambda: SimpleNamespace(
            status=lambda: {
                'ready': False,
                'inventory_ok': True,
                'api_probe': {'status_code': 429, 'error': 'quota exceeded'},
                'auth_probe': {'stderr': 'quota exceeded'},
                'models_probe': {'stderr': ''},
            }
        ),
    )
    monkeypatch.setattr(
        'core.agents.antigravity_cli_agent.MimoAgent',
        lambda *args, **kwargs: SimpleNamespace(health=lambda: SimpleNamespace(status=AgentStatus.READY)),
    )
    monkeypatch.setattr(
        'core.agents.antigravity_cli_agent.AIKernelAgent',
        lambda *args, **kwargs: SimpleNamespace(health=lambda: SimpleNamespace(status=AgentStatus.FAILED)),
    )
    agent = AntigravityCLIAgent('antigravity-1')

    health = agent.health()

    assert health.status == AgentStatus.READY
    assert 'mimo_fallback' in str(health.last_error)


def test_antigravity_cli_success_uses_direct_api_bridge(monkeypatch):
    agent = _agent()

    monkeypatch.setattr(
        ExternalAIBridge,
        'run_antigravity',
        lambda self, task, prompt, timeout_sec=120: BridgeExecResult(True, 'ok', '', 'antigravity', 'antigravity-pro', 1, error_type='none'),
    )

    result = agent.run(_task())

    assert result.status == TaskStatus.DONE
    assert result.output['summary'] == 'ok'


def test_antigravity_cli_timeout_returns_failed(monkeypatch):
    monkeypatch.setenv('AI_BRIDGE_ANTIGRAVITY_ENABLE_PROVIDER_FALLBACK', 'false')
    agent = _agent()

    monkeypatch.setattr(
        ExternalAIBridge,
        'run_antigravity',
        lambda self, task, prompt, timeout_sec=120: BridgeExecResult(False, '', 'request timeout', 'antigravity', 'antigravity-pro', 1, error_type='api_timeout'),
    )

    result = agent.run(_task())

    assert result.status == TaskStatus.FAILED
    assert 'timed out' in result.output['summary'].lower()
    assert agent.active_tasks == 0


def test_antigravity_cli_surfaces_provider_fallback_result(monkeypatch):
    agent = _agent()

    monkeypatch.setattr(
        ExternalAIBridge,
        'run_antigravity',
        lambda self, task, prompt, timeout_sec=120: BridgeExecResult(True, 'mimo ok', '', 'mimo', 'xiaomi/mimo-v2.5-pro', 1, error_type='provider_fallback'),
    )

    result = agent.run(_task())

    assert result.status == TaskStatus.DONE
    assert result.provider == 'mimo'
    assert result.model_name == 'xiaomi/mimo-v2.5-pro'
    assert result.output['summary'].startswith('[antigravity->mimo-fallback]')
