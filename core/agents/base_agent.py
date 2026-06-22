from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from core.core.host_bridge import HostBridge
from core.core.kernel_api import KernelAPI
from core.core.models import AgentHealth, AgentResult, AgentStatus, ResultOutput, Task, TaskStatus




@runtime_checkable
class PromptRecorder(Protocol):
    def record_execution_prompt(
        self,
        task: Task,
        *,
        agent_id: str,
        provider: str,
        model_name: str,
        prompt: str,
        memory_context: dict | None = None,
    ) -> None:
        ...

class BaseAgent(ABC):
    def __init__(self, agent_id: str, capabilities: list[str]) -> None:
        self.agent_id = agent_id
        self.capabilities = capabilities
        self.provider = "local"
        self.model_name = "unknown"
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

    def set_identity(self, *, provider: str, model_name: str) -> None:
        self.provider = provider
        self.model_name = model_name
        # Keep legacy fields in sync while older modules still reference them.
        self._provider = provider
        self._model = model_name

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

    @staticmethod
    def _trusted_memory_summary(memory_context: dict | None = None, *, max_chars: int = 180) -> str:
        memory = memory_context or {}
        if not memory.get("trained_memory_trusted"):
            return ""
        brief = str(memory.get("trained_memory_brief", "") or "").strip()
        if len(brief) < 40:
            return ""
        return brief[:max_chars]

    def _record_execution_prompt(self, task: Task, prompt: str, memory_context: dict | None = None, *, provider: str | None = None, model_name: str | None = None) -> None:
        api = self.get_api()
        layered = api.get_context("layered_context_memory") if api and hasattr(api, "get_context") else None
        if isinstance(layered, PromptRecorder):
            try:
                layered.record_execution_prompt(
                    task,
                    agent_id=self.agent_id,
                    provider=provider or self.provider,
                    model_name=model_name or self.model_name,
                    prompt=prompt,
                    memory_context=memory_context,
                )
            except Exception as exc:
                # Prompt journaling must not break task execution, but keep the
                # agent health surface honest when telemetry recording fails.
                self.last_error = f"prompt_record_failed: {exc}"

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
        resolved_provider = provider if provider is not None else self.provider
        resolved_model_name = model_name if model_name is not None else self.model_name
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
