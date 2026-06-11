from __future__ import annotations

from abc import ABC, abstractmethod

from core.core.host_bridge import HostBridge
from core.core.models import AgentHealth, AgentResult, AgentStatus, ResultOutput, Task, TaskStatus


class BaseAgent(ABC):
    def __init__(self, agent_id: str, capabilities: list[str]) -> None:
        self.agent_id = agent_id
        self.capabilities = capabilities
        self.active_tasks = 0
        self.queue_depth = 0
        self.avg_latency_ms = 0.0
        self.success_rate = 1.0
        self.last_error: str | None = None
        self.host_bridge: HostBridge | None = None

    def health(self) -> AgentHealth:
        return AgentHealth(
            agent_id=self.agent_id,
            status=AgentStatus.BUSY if self.active_tasks else AgentStatus.READY,
            capabilities=self.capabilities,
            active_tasks=self.active_tasks,
            queue_depth=self.queue_depth,
            avg_latency_ms=self.avg_latency_ms,
            success_rate=self.success_rate,
            last_error=self.last_error,
        )

    def set_host_bridge(self, bridge: HostBridge) -> None:
        self.host_bridge = bridge

    @abstractmethod
    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        raise NotImplementedError

    def execute(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        return self.run(task, memory_context=memory_context)

    def healthcheck(self) -> AgentHealth:
        return self.health()

    def result(
        self,
        task: Task,
        summary: str,
        status: TaskStatus = TaskStatus.DONE,
        confidence: float = 0.9,
        errors: list[str] | None = None,
        *,
        provider: str | None = None,
        model_name: str | None = None,
        output: ResultOutput | dict | None = None,
    ) -> AgentResult:
        result_output = output if output is not None else ResultOutput(
            summary=summary,
            files_changed=[],
            commands_run=[],
            test_results=[],
            diff="",
        )
        resolved_provider = provider if provider is not None else getattr(self, "provider", None) or getattr(self, "_provider", None)
        resolved_model_name = model_name if model_name is not None else getattr(self, "model_name", None) or getattr(self, "_model", None)
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            status=status,
            output=result_output,
            confidence=confidence,
            errors=errors or [],
            next_recommendations=[],
            provider=resolved_provider,
            model_name=resolved_model_name,
        )
