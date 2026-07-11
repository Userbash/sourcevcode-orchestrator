from __future__ import annotations

from typing import Any

from core.agents.base_agent import BaseAgent
from core.core.analytics_matrix_engine import AnalyticsKnowledgePool, AnalyticsMatrixEngine
from core.core.models import AgentResult, ResultOutput, Task


class DataAnalyticsMatrixAgent(BaseAgent):
    def __init__(self, agent_id: str = "data-analytics-matrix-agent", *, generator: Any | None = None) -> None:
        super().__init__(agent_id, ["analytics", "data_science", "retrieval", "knowledge_synthesis"])
        self.set_identity(provider="local", model_name="analytics-matrix-v1")
        self.engine = AnalyticsMatrixEngine(generator=generator)
        self.knowledge_pool = AnalyticsKnowledgePool()

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        description = getattr(task.input, "description", "") or ""
        memory_brief = self._memory_brief(memory_context)
        joined_text = description if not memory_brief else f"{description}\n\nMemory:\n{memory_brief}"
        metadata = {
            "task_id": task.task_id,
            "project": getattr(task.context, "project", None),
            "files": list(getattr(task.input, "files", []) or []),
        }
        report = self.engine.analyze(joined_text, metadata=metadata, knowledge_pool=self.knowledge_pool)
        self.knowledge_pool.ingest(report, source_id=task.task_id, metadata=metadata)
        summary = report.generated_text or "Analytics matrix report generated."
        output = ResultOutput(
            summary=summary,
            files_changed=[],
            commands_run=[],
            test_results=[],
            diff=None,
        )
        output.analytics_matrix = report.as_dict()
        output.knowledge_pool = self.knowledge_pool.snapshot()
        return self.result(task, summary, output=output, confidence=0.88)
