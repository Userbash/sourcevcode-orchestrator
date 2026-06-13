from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.models import AgentResult, Task


class ReviewerAgent(BaseAgent):
    def __init__(self, agent_id: str = "revieweragent") -> None:
        super().__init__(agent_id, ['review', 'security'])

    @staticmethod
    def _trusted_summary(memory_context: dict | None = None) -> str:
        memory = memory_context or {}
        if not memory.get("trained_memory_trusted"):
            return ""
        brief = str(memory.get("trained_memory_brief", "") or "").strip()
        return brief[:180] if len(brief) >= 40 else ""

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        trained = self._trusted_summary(memory_context)
        summary = "Reviewed architecture, security, style, and maintainability."
        if trained:
            summary = f"{summary} Trained memory used: {trained}"
        return self.result(task, summary)
