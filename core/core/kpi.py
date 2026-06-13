from __future__ import annotations

from .models import AgentKPI, AgentRecord, Task, TaskType


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
        cost_efficiency = max(0.0, min(1.0, 1.0 / (1.0 + agent.metrics.estimated_cost)))
        delivery = max(0.0, min(1.0, 1000.0 / (1000.0 + agent.metrics.avg_latency_ms)))
        aggregate = (delivery + agent.metrics.quality_score + stability + cost_efficiency + reuse + agent.metrics.test_pass_rate + agent.metrics.review_score) / 7.0
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
