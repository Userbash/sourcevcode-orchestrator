from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.models import AgentResult, Task


class DocsAgent(BaseAgent):
    def __init__(self, agent_id: str = "docsagent") -> None:
        super().__init__(agent_id, ['docs'])

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        return self.result(task, "Updated project documentation and usage examples.")
