from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.models import AgentResult, Task

__test__ = False


class TesterAgent(BaseAgent):
    __test__ = False

    def __init__(self, agent_id: str = "testeragent") -> None:
        super().__init__(agent_id, ["test", "ci"])
        self.set_identity(provider="openai", model_name="gpt-test-standard")

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        trained = self._trusted_memory_summary(memory_context)
        summary = "Tester placeholder response generated; no test commands were executed and no runtime assertions were verified."
        if trained:
            summary = f"{summary} Trained memory used: {trained}"
        return self.result(task, summary)
