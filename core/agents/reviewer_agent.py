from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.models import AgentResult, Task


class ReviewerAgent(BaseAgent):
    def __init__(self, agent_id: str = "revieweragent") -> None:
        super().__init__(agent_id, ["review", "security"])
        self.set_identity(provider="openai", model_name="gpt-review-large")

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        trained = self._trusted_memory_summary(memory_context)
        summary = "Reviewer placeholder response generated; no static analysis, diff inspection, or security validation was performed."
        if trained:
            summary = f"{summary} Trained memory used: {trained}"
        return self.result(task, summary)
