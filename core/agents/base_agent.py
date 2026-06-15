from __future__ import annotations

from abc import ABC, abstractmethod
from core.core.host_bridge import HostBridge
from core.core.kernel_api import KernelAPI
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
        self._api: KernelAPI | None = None

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

    def set_api(self, api: KernelAPI) -> None:
        self._api = api

    def get_api(self) -> KernelAPI | None:
        return self._api

    @staticmethod
    def _memory_brief(memory_context: dict | None = None, *, max_chars: int = 1600) -> str:
        if not isinstance(memory_context, dict) or not memory_context:
            return ""
        lines: list[str] = []
        for label, key in (
            ("LAYERED CONTEXT", "layered_context_brief"),
            ("TRAINED MEMORY", "trained_memory_brief"),
            ("REUSABLE TASK MEMORY", "reusable_task_memory_brief"),
            ("PROMPT MEMORY", "prompt_memory_brief"),
        ):
            value = str(memory_context.get(key) or "").strip()
            if value:
                lines.append(f"{label}: {value}")
        handoffs = memory_context.get("handoff_summaries")
        if isinstance(handoffs, list):
            compact = [str(item).strip() for item in handoffs if str(item).strip()]
            if compact:
                lines.append(f"P2P HANDOFFS: {' | '.join(compact[:3])}")
        guidance = memory_context.get("prompt_guidance")
        if isinstance(guidance, list):
            compact = [str(item).strip() for item in guidance if str(item).strip()]
            if compact:
                lines.append(f"PROMPT GUIDANCE: {' | '.join(compact[:4])}")
        text = "\n".join(lines).strip()
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)].rstrip() + "..."

    def _record_execution_prompt(self, task: Task, prompt: str, memory_context: dict | None = None, *, provider: str | None = None, model_name: str | None = None) -> None:
        api = self.get_api()
        layered = api.get_context("layered_context_memory") if api and hasattr(api, "get_context") else None
        if layered and hasattr(layered, "record_execution_prompt"):
            try:
                layered.record_execution_prompt(
                    task,
                    agent_id=self.agent_id,
                    provider=provider or getattr(self, "provider", None) or getattr(self, "_provider", "local"),
                    model_name=model_name or getattr(self, "model_name", None) or getattr(self, "_model", "unknown"),
                    prompt=prompt,
                    memory_context=memory_context,
                )
            except Exception:
                pass

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
