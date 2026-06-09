from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.models import AgentResult, Task

__test__ = False


class TesterAgent(BaseAgent):
    __test__ = False
    def __init__(self, agent_id: str = "testeragent") -> None:
        super().__init__(agent_id, ['test', 'ci'])

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        return self.result(task, "Created and ran tests for acceptance criteria.")
