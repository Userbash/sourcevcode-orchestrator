from __future__ import annotations

from datetime import UTC, datetime

from .models import AgentRecord, AgentStatus, Priority, Task
from .model_value import compute_model_value
from .inventory_scoring_policy import InventoryScoringPolicy


UNROUTABLE_AGENT_STATUSES = {
    AgentStatus.OFFLINE,
    AgentStatus.DISABLED,
    AgentStatus.FAILED,
    AgentStatus.OVERLOADED,
}


def agent_load_ratio(agent: AgentRecord) -> float:
    limit = float(agent.limits.get("max_active_tasks", 5) or 5)
    return (agent.metrics.active_tasks + agent.metrics.queue_depth) / limit


def agent_accepts_task_priority(agent: AgentRecord, priority: Priority | str | None) -> bool:
    if agent.status == AgentStatus.BUSY:
        return priority == Priority.LOW or priority == Priority.LOW.value
    return True


def is_agent_routable(agent: AgentRecord, priority: Priority | str | None = None) -> bool:
    if agent.status in UNROUTABLE_AGENT_STATUSES:
        return False
    if float(agent.metrics.error_rate or 0.0) > 0.5:
        return False
    if float(agent.metrics.priority_score or 0.0) <= 0.0:
        return False
    if agent_load_ratio(agent) > 1:
        agent.status = AgentStatus.OVERLOADED
        agent.metrics.status = agent.status
        return False
    return agent_accepts_task_priority(agent, priority)


class LoadBalancer:
    def __init__(self, overload_threshold: float = 0.85) -> None:
        self.overload_threshold = overload_threshold
        self._inventory_snapshot_source = None
        self._model_lookup_source = None
        self._agent_runtime_source = None

    def set_inventory_sources(self, *, runtime_inventory_source=None, model_lookup_source=None) -> None:
        self._inventory_snapshot_source = runtime_inventory_source
        self._model_lookup_source = model_lookup_source

    def set_runtime_event_source(self, *, agent_runtime_source=None) -> None:
        self._agent_runtime_source = agent_runtime_source

    def score(self, agent: AgentRecord, capability: str, priority: Priority | str | None = None, task: Task | None = None) -> float:
        if not is_agent_routable(agent, priority):
            return float("-inf")
        runtime_agent = {}
        if callable(self._agent_runtime_source):
            try:
                runtime_agent = self._agent_runtime_source(str(agent.id)) or {}
            except Exception:
                runtime_agent = {}
        runtime_status = str(runtime_agent.get("status") or "").strip().lower()
        if runtime_status in {"offline", "failed", "suppressed", "overloaded"}:
            return float("-inf")
        
        # Calibration formula (Section 6): 
        # quality * 0.30 + success * 0.25 + review * 0.15 + avail * 0.10 + latency * 0.10 + cost * 0.05 + spec * 0.05
        
        quality_score = max(0.0, min(1.0, agent.metrics.quality_score))
        success_rate = max(0.0, min(1.0, agent.metrics.success_rate))
        review_pass_rate = max(0.0, min(1.0, agent.metrics.review_score))
        test_pass_rate = max(0.0, min(1.0, agent.metrics.test_pass_rate))
        
        availability = self._availability(agent)
        specialization_score = 1.0 if capability in agent.capabilities else 0.0
        memory_efficiency = float(getattr(agent.metrics, "memory_efficiency", getattr(agent.kpi, "reuse_score", 1.0)) or 1.0)
        value = compute_model_value(
            success_rate=success_rate,
            quality_score=max(0.0, min(1.0, (quality_score + review_pass_rate + test_pass_rate) / 3.0)),
            latency_ms=agent.metrics.avg_latency_ms,
            cost_usd=float(agent.metrics.estimated_cost or agent.metrics.token_cost or 0.0),
            memory_efficiency=memory_efficiency,
            availability=availability,
            specialization=specialization_score,
            context_fit=0.5,
        )
        speed_score = float(value["components"]["latency_score"])
        cost_score = float(value["components"]["cost_efficiency"])
        
        overload_penalty = self._overload_penalty(agent)
        runtime_entry = {}
        if callable(self._inventory_snapshot_source):
            try:
                runtime_entry = self._inventory_snapshot_source(str(agent.provider or "")) or {}
            except Exception:
                runtime_entry = {}
        model_row = {}
        if callable(self._model_lookup_source):
            try:
                model_row = self._model_lookup_source(str(agent.model_name or "")) or {}
            except Exception:
                model_row = {}
        inventory_bonus = InventoryScoringPolicy.lane_bonus(
            provider=str(agent.provider or ""),
            runtime_entry=runtime_entry,
            model_row=model_row,
            model_name=str(agent.model_name or ""),
        )
        secure_bonus = 0.0
        runtime_penalty = 0.0
        if runtime_status == "degraded":
            runtime_penalty = 0.2
        elif runtime_status == "busy":
            runtime_penalty = 0.1
        if priority in {Priority.HIGH, Priority.CRITICAL, "high", "critical"}:
            secure_hint = f"{agent.id} {agent.model_name}".lower()
            if any(token in secure_hint for token in ("secure", "senior")):
                secure_bonus = 0.12

        quality_component = float(value["value_score"]) * 0.62
        kpi_component = getattr(agent.kpi, "agent_kpi", 1.0) * 0.14
        if task is not None:
            try:
                from .kpi import KPIEvaluator
                kpi_evaluator = KPIEvaluator()
                threshold = kpi_evaluator.threshold_for_task(task)
                floor = float((getattr(task, "routing_hints", {}) or {}).get("kpi_floor", threshold))
                effective_threshold = max(threshold, floor)
                if agent.kpi.agent_kpi < effective_threshold:
                    kpi_component -= (effective_threshold - agent.kpi.agent_kpi) * 0.25
            except Exception:
                pass
        return (
            quality_component
            + success_rate * 0.18
            + kpi_component
            + availability * 0.09
            + speed_score * 0.08
            + cost_score * 0.04
            + specialization_score * 0.05
            + secure_bonus
            + inventory_bonus
            - overload_penalty
            - runtime_penalty
        ) * agent.metrics.priority_score

    async def score_async(self, agent: AgentRecord, capability: str, priority: Priority | str | None = None, task: Task | None = None) -> float:
        """Asynchronous version of score, allowing for external health checks or IO."""
        # For now, it just calls the sync version, but we wrap it to maintain the interface
        return self.score(agent, capability, priority, task)

    async def choose_async(self, agents: list[AgentRecord], capability: str, priority: Priority | str | None = None, task: Task | None = None) -> AgentRecord | None:
        """Concurrently scores all candidates and picks the best one."""
        import asyncio
        candidates = [
            agent for agent in agents
            if capability in agent.capabilities and is_agent_routable(agent, priority)
        ]
        if not candidates:
            return None
            
        scores = await asyncio.gather(*[
            self.score_async(agent, capability, priority, task) for agent in candidates
        ])
        
        indexed_scores = list(zip(candidates, scores))
        valid_indexed = [(a, s) for a, s in indexed_scores if s != float("-inf")]
        
        if not valid_indexed:
            return None
            
        return max(valid_indexed, key=lambda x: x[1])[0]

    def choose(self, agents: list[AgentRecord], capability: str, priority: Priority | str | None = None, task: Task | None = None) -> AgentRecord | None:
        candidates = [
            agent for agent in agents
            if capability in agent.capabilities and is_agent_routable(agent, priority)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda agent: self.score(agent, capability, priority, task))

    def _availability(self, agent: AgentRecord) -> float:
        if agent.status in {AgentStatus.READY, AgentStatus.IDLE}:
            base = 1.0
        elif agent.status == AgentStatus.DEGRADED:
            base = 0.45
        elif agent.status == AgentStatus.STARTING:
            base = 0.35
        elif agent.status == AgentStatus.BUSY:
            base = 0.2
        elif agent.status == AgentStatus.OVERLOADED:
            base = 0.1
        else:
            base = 0.0
        minutes_since_seen = max(0.0, (datetime.now(UTC) - agent.last_seen).total_seconds() / 60)
        return max(0.0, base - min(0.5, minutes_since_seen / 120))

    @staticmethod
    def _speed_score(avg_latency_ms: float) -> float:
        if avg_latency_ms <= 0:
            return 1.0
        return max(0.0, min(1.0, 1000.0 / (1000.0 + avg_latency_ms)))

    @staticmethod
    def _cost_score(cost: float) -> float:
        return max(0.0, min(1.0, 1.0 / (1.0 + cost)))

    def _overload_penalty(self, agent: AgentRecord) -> float:
        load = agent_load_ratio(agent)
        if load > 1:
            agent.status = AgentStatus.OVERLOADED
            agent.metrics.status = agent.status
        if load <= self.overload_threshold:
            return 0.0
        return min(0.8, load - self.overload_threshold)
