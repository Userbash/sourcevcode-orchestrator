from __future__ import annotations

from types import SimpleNamespace

from core.agents.antigravity_cli_agent import AntigravityCLIAgent
from core.core.models import AgentStatus


def test_antigravity_cli_agent_health_reflects_manager_degradation(monkeypatch):
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
