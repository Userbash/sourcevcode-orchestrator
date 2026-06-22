from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.models import AgentResult, Task


class DocsAgent(BaseAgent):
    def __init__(self, agent_id: str = "docsagent") -> None:
        super().__init__(agent_id, ["docs"])
        self.set_identity(provider="openai", model_name="gpt-docs")

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        return self.result(
            task,
            "Docs placeholder response generated; no documentation files were inspected or edited.",
        )
