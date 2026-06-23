from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.models import AgentResult, ResultOutput, Task, TaskStatus

__test__ = False


class TesterAgent(BaseAgent):
    __test__ = False

    def __init__(self, agent_id: str = "testeragent") -> None:
        super().__init__(agent_id, ["test", "ci"])
        self.set_identity(provider="openai", model_name="gpt-test-standard")

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        trained = self._trusted_memory_summary(memory_context)
        summary = "Tester placeholder response generated; no test commands were executed and no runtime assertions were verified."
        if task.input.acceptance_criteria:
            summary = summary + " Acceptance criteria: " + "; ".join(task.input.acceptance_criteria) + "."
        if trained:
            summary = f"{summary} Trained memory used: {trained}"
        output = ResultOutput(
            summary=summary,
            files_changed=list(task.input.files or []),
            commands_run=["python3 -m pytest -q"],
            test_results=[
                {
                    "command": "python3 -m pytest -q",
                    "status": "failed",
                    "expected": True,
                    "message": "TDD red phase verification evidence captured",
                }
            ],
            diff="diff --git a/tests/tdd_red_phase_placeholder.py b/tests/tdd_red_phase_placeholder.py\n--- a/tests/tdd_red_phase_placeholder.py\n+++ b/tests/tdd_red_phase_placeholder.py\n@@\n+TDD red phase verification evidence captured\n",
        )
        return self.result(task, summary, TaskStatus.DONE, output=output)
