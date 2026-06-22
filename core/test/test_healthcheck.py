from core.core.agent_registry import AgentRegistry
from core.core.healthcheck import HealthChecker
from core.core.models import AgentHealth, AgentStatus


def test_local_healthcheck_agent():
    registry = AgentRegistry()
    registry.register("codex-main", "codex", "local://codex", ["code", "fix"])

    health = HealthChecker(registry).check_agent("codex-main")

    assert health.agent_id == "codex-main"
    assert health.status.value == "ready"
    assert "code" in health.capabilities


def test_local_healthcheck_uses_live_resolver():
    registry = AgentRegistry()
    registry.register("ai-kernel-qwen36-1", "custom", "local://ai-kernel-qwen36-1", ["code", "fix"])
    checker = HealthChecker(registry)
    checker.set_local_health_resolver(
        lambda agent_id: AgentHealth(
            agent_id=agent_id,
            status=AgentStatus.FAILED,
            capabilities=["code", "fix"],
            last_error="connection refused",
        )
    )

    health = checker.check_agent("ai-kernel-qwen36-1")

    assert health.status == AgentStatus.FAILED
    assert health.last_error == "connection refused"
