from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from core.core.agent_registry import AgentRegistry
from core.core.load_balancer import is_agent_routable
from core.core.models import AgentHealth, AgentStatus
from core.core.orchestrator import Orchestrator


class _AvailabilityStub:
    def __init__(self, report=None) -> None:
        self._report = report or {}

    def cached_report(self):
        return self._report


class _HealthcheckStub:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self._calls = 0

    def check_agent(self, agent_id: str):
        response = self._responses[min(self._calls, len(self._responses) - 1)]
        self._calls += 1
        if isinstance(response, Exception):
            raise response
        return response


def _make_orchestrator(registry: AgentRegistry) -> Orchestrator:
    orchestrator = object.__new__(Orchestrator)
    orchestrator.registry = registry
    orchestrator.local_agents = {}
    orchestrator._agent_suppression_ttl_sec = 300
    orchestrator._agent_probe_failures = {}
    orchestrator._agent_suppressed_until = {}
    orchestrator._agent_recent_errors = defaultdict(list)
    orchestrator._agent_last_probe = {}
    orchestrator.availability = _AvailabilityStub()
    orchestrator.healthcheck = _HealthcheckStub([])
    orchestrator._normalize_provider = lambda provider: str(provider or "").strip().lower()
    return orchestrator


def test_probe_agent_runtime_two_failures_suppresses_lane():
    registry = AgentRegistry()
    agent = registry.register("codex-main", "codex", "remote://codex-main", ["code"], provider="openai")
    orchestrator = _make_orchestrator(registry)
    orchestrator.healthcheck = _HealthcheckStub(
        [
            RuntimeError("probe failed"),
            RuntimeError("probe failed again"),
        ]
    )

    first = orchestrator.probe_agent_runtime(agent.id)
    second = orchestrator.probe_agent_runtime(agent.id)

    assert first["status"] == AgentStatus.DEGRADED.value
    assert second["status"] == AgentStatus.OFFLINE.value
    assert agent.status == AgentStatus.OFFLINE
    assert agent.metrics.priority_score == 0.0
    assert agent.id in orchestrator._agent_suppressed_until


def test_probe_agent_runtime_recovers_after_successful_probe():
    registry = AgentRegistry()
    agent = registry.register("codex-main", "codex", "remote://codex-main", ["code"], provider="openai")
    orchestrator = _make_orchestrator(registry)
    orchestrator.suppress_lane(agent.id, reason="temporary_failure", seconds=60)
    orchestrator._agent_suppressed_until[agent.id] = datetime.now(UTC) - timedelta(seconds=1)
    orchestrator.healthcheck = _HealthcheckStub(
        [
            AgentHealth(
                agent_id=agent.id,
                status=AgentStatus.READY,
                capabilities=["code"],
            )
        ]
    )

    result = orchestrator.probe_agent_runtime(agent.id)

    assert result["ok"] is True
    assert result["status"] == AgentStatus.READY.value
    assert agent.status == AgentStatus.READY
    assert agent.metrics.priority_score == 1.0
    assert agent.id not in orchestrator._agent_suppressed_until


def test_probe_agent_runtime_marks_slow_lane_degraded(monkeypatch):
    registry = AgentRegistry()
    agent = registry.register("reviewer-1", "reviewer", "remote://reviewer-1", ["review"], provider="openai")
    orchestrator = _make_orchestrator(registry)
    orchestrator.healthcheck = _HealthcheckStub(
        [
            AgentHealth(
                agent_id=agent.id,
                status=AgentStatus.READY,
                capabilities=["review"],
            )
        ]
    )
    monotonic_values = iter([100.0, 106.5])
    monkeypatch.setattr("core.core.orchestrator.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setenv("AI_BRIDGE_AGENT_DEGRADED_LATENCY_MS", "5000")

    result = orchestrator.probe_agent_runtime(agent.id)

    assert result["ok"] is True
    assert result["status"] == AgentStatus.DEGRADED.value
    assert agent.status == AgentStatus.DEGRADED
    assert agent.metrics.priority_score == 0.35


def test_registry_reconcile_suppresses_zombie_local_agent():
    registry = AgentRegistry()
    zombie = registry.register("zombie-agent", "custom", "local://zombie-agent", ["docs"], provider="local")
    orchestrator = _make_orchestrator(registry)

    summary = orchestrator.registry_reconcile()

    assert summary["zombies"] == ["zombie-agent"]
    assert summary["suppressed"] == ["zombie-agent"]
    assert zombie.status == AgentStatus.OFFLINE
    assert zombie.metrics.priority_score == 0.0


def test_load_balancer_rejects_suppressed_lane_by_priority_score():
    registry = AgentRegistry()
    agent = registry.register("suppressed-agent", "custom", "remote://suppressed-agent", ["code"], provider="openai")
    orchestrator = _make_orchestrator(registry)
    orchestrator.suppress_lane(agent.id, reason="offline")

    assert is_agent_routable(agent) is False
