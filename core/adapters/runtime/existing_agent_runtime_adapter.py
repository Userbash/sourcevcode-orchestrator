from __future__ import annotations

from typing import Protocol

from core.core.models import AgentResult, Task, TaskStatus
from core.core.ports.agent_runtime import RuntimeContext
from core.core.task_router import CAPABILITY_BY_TASK_TYPE


class SupportsAgentDelivery(Protocol):
    local_agents: dict[str, object]

    def _run_local_agent_via_delivery(
        self,
        task: Task,
        agent_id: str,
        capability: str,
        agent: object,
        memory_context: dict[str, object],
    ) -> AgentResult:
        ...


class ExistingAgentRuntimeAdapter:
    def __init__(self, orchestrator: SupportsAgentDelivery) -> None:
        self.orchestrator = orchestrator

    def execute(self, agent_id: str, task: Task, context: RuntimeContext) -> AgentResult:
        agent = self.orchestrator.local_agents.get(agent_id)
        if agent is None:
            return AgentResult(
                task_id=task.task_id,
                agent_id=agent_id,
                status=TaskStatus.FAILED,
                output={"summary": "Local agent is not registered"},
                confidence=0.0,
                errors=["agent_not_registered"],
                next_recommendations=[],
            )

        capability = task.required_capability or CAPABILITY_BY_TASK_TYPE[task.type]
        return self.orchestrator._run_local_agent_via_delivery(
            task,
            agent_id,
            capability,
            agent,
            dict(context),
        )
