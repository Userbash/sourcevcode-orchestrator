from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.models import AgentResult, Task


class PlannerAgent(BaseAgent):
    def __init__(self, agent_id: str = "planneragent") -> None:
        super().__init__(agent_id, ["plan"])
        self.set_identity(provider="openai", model_name="gpt-planner")

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        trained = self._trusted_memory_summary(memory_context)
        summary = "Planner placeholder response generated; no files were inspected and no execution plan was validated against repository state."
        if trained:
            summary = f"{summary} Trained memory used: {trained}"
        return self.result(task, summary)
