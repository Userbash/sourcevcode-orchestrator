from __future__ import annotations

from .models import AgentKPI, AgentRecord, Task, TaskType
from .model_value import compute_model_value


class KPIEvaluator:
    def __init__(self, threshold: float = 0.65, task_thresholds: dict[str, float] | None = None) -> None:
        self.threshold = threshold
        self.task_thresholds = task_thresholds or {
            "plan": 0.72,
            "review": 0.76,
            "test": 0.74,
        }

    def calculate(self, agent: AgentRecord) -> AgentKPI:
        total = agent.metrics.completed_tasks + agent.metrics.failed_tasks
        stability = 1.0 - agent.metrics.error_rate
        reuse = min(1.0, total / 10) if total else 0.0
        memory_efficiency = float(getattr(agent.metrics, "memory_efficiency", reuse) or reuse)
        quality_blend = max(0.0, min(1.0, (agent.metrics.quality_score + agent.metrics.review_score + agent.metrics.test_pass_rate) / 3.0))
        value = compute_model_value(
            success_rate=agent.metrics.success_rate,
            quality_score=quality_blend,
            latency_ms=agent.metrics.avg_latency_ms,
            cost_usd=float(agent.metrics.estimated_cost or agent.metrics.token_cost or 0.0),
            memory_efficiency=memory_efficiency,
            availability=1.0 if str(agent.status.value if hasattr(agent.status, "value") else agent.status) in {"ready", "idle", "degraded"} else 0.3,
            specialization=1.0,
            context_fit=0.5,
        )
        cost_efficiency = float(value["components"]["cost_efficiency"])
        delivery = float(value["components"]["latency_score"])
        aggregate = float(value["value_score"])
        agent.kpi = AgentKPI(
            agent_id=agent.id,
            agent_kpi=aggregate,
            delivery_score=delivery,
            quality_score=agent.metrics.quality_score,
            stability_score=stability,
            cost_efficiency=cost_efficiency,
            reuse_score=reuse,
            test_success_rate=agent.metrics.test_pass_rate,
            review_pass_rate=agent.metrics.review_score,
            efficiency=delivery,
            reliability=stability,
            efficiency_score=aggregate,
            error_rate=agent.metrics.error_rate,
        )
        return agent.kpi

    def below_threshold(self, agent: AgentRecord) -> bool:
        return self.calculate(agent).agent_kpi < self.threshold

    def threshold_for_task(self, task: Task | None) -> float:
        if not task:
            return self.threshold
        task_type = getattr(task.type, "value", task.type)
        return float(self.task_thresholds.get(str(task_type).lower(), self.threshold))

    def below_task_threshold(self, agent: AgentRecord, task: Task | None) -> bool:
        return self.calculate(agent).agent_kpi < self.threshold_for_task(task)

    def apply_priority_policy(self, agent: AgentRecord) -> None:
        if self.below_threshold(agent):
            agent.metrics.priority_score = max(0.25, agent.metrics.priority_score * 0.7)
