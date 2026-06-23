from __future__ import annotations

from types import SimpleNamespace

from core.scripts.orchestrator_daemon import _attach_optional_local_agent
from core.core.models import AgentStatus


class _FakeAgent:
    def __init__(self, status: str, last_error: str | None = None) -> None:
        self._status = status
        self._last_error = last_error

    def health(self):
        return SimpleNamespace(status=SimpleNamespace(value=self._status), last_error=self._last_error)


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def attach_local_agent(self, agent_id, agent, *, agent_type, critical=False, model_name='unknown', provider='local'):
        self.calls.append((agent_id, model_name, provider))


def test_attach_optional_local_agent_skips_failed_agent(monkeypatch):
    monkeypatch.delenv('AI_BRIDGE_ATTACH_OPTIONAL_DEGRADED_AGENTS', raising=False)
    orchestrator = _FakeOrchestrator()

    attached = _attach_optional_local_agent(
        orchestrator,
        'ai-kernel-qwen36-1',
        _FakeAgent('failed', 'connection refused'),
        agent_type='custom',
        critical=False,
        model_name='hauhaucs-qwen36',
        provider='ai_kernel',
    )

    assert attached is False
    assert orchestrator.calls == []


def test_attach_optional_local_agent_allows_ready_agent():
    orchestrator = _FakeOrchestrator()

    attached = _attach_optional_local_agent(
        orchestrator,
        'mistral-1',
        _FakeAgent('ready'),
        agent_type='external_ai',
        critical=False,
        model_name='mistral-large-latest',
        provider='mistral',
    )

    assert attached is True
    assert orchestrator.calls == [('mistral-1', 'mistral-large-latest', 'mistral')]


def test_attach_optional_local_agent_can_allow_degraded_by_env(monkeypatch):
    monkeypatch.setenv('AI_BRIDGE_ATTACH_OPTIONAL_DEGRADED_AGENTS', 'true')
    orchestrator = _FakeOrchestrator()

    attached = _attach_optional_local_agent(
        orchestrator,
        'antigravity-1',
        _FakeAgent('degraded', 'api blocked'),
        agent_type='external_ai',
        critical=False,
        model_name='antigravity-pro',
        provider='antigravity',
    )

    assert attached is True
    assert orchestrator.calls == [('antigravity-1', 'antigravity-pro', 'antigravity')]
