from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.env_loader import load_env_file
from core.core.mimo_provider import extract_mimo_response_text, invoke_mimo_native, is_mimo_auto_router, is_native_mimo_model, normalize_mimo_model_name
from core.core.mimo_status import build_mimo_runtime_status, mimo_enabled
from core.core.models import AgentHealth, AgentStatus, Task, TaskStatus


class MimoAgent(BaseAgent):
    def __init__(self, agent_id: str = "mimo-router-1", default_model: str = "xiaomi/mimo-v2.5-pro") -> None:
        super().__init__(agent_id, ["code", "fix", "test", "review", "docs", "research", "plan", "analysis", "summarization"])
        load_env_file()
        load_env_file(".env.bridge", override=True)
        load_env_file(".env.local.secrets", override=True)
        load_env_file(".env.gemini.local", override=True)
        self.set_identity(provider="mimo", model_name=default_model)

    def health(self) -> AgentHealth:
        if not mimo_enabled():
            return AgentHealth(agent_id=self.agent_id, status=AgentStatus.FAILED, capabilities=self.capabilities, active_tasks=self.active_tasks, queue_depth=self.queue_depth, avg_latency_ms=self.avg_latency_ms, success_rate=0.0, last_error='mimo_disabled_by_env')
        snapshot = build_mimo_runtime_status()
        status_raw = str(snapshot.get("status") or "").strip().lower()
        if bool(snapshot.get("ready")):
            status = AgentStatus.DEGRADED if status_raw == "degraded" else AgentStatus.READY
        elif status_raw in {"degraded", "inventory_unknown"}:
            status = AgentStatus.DEGRADED
        else:
            status = AgentStatus.FAILED
        last_error = None
        failed = snapshot.get("failed_models_sample")
        if isinstance(failed, list) and failed:
            first = failed[0]
            if isinstance(first, dict):
                last_error = str(first.get("error") or "").strip() or None
        if not last_error:
            last_error = str(snapshot.get("failure_reason") or snapshot.get("live_inventory_error") or "").strip() or None
        return AgentHealth(agent_id=self.agent_id, status=status, capabilities=self.capabilities, active_tasks=self.active_tasks, queue_depth=self.queue_depth, avg_latency_ms=self.avg_latency_ms, success_rate=1.0 if status == AgentStatus.READY else 0.0, last_error=last_error)

    def run(self, task: Task, memory_context: dict | None = None):
        if not mimo_enabled():
            return self.result(task, 'MIMO is disabled by environment', TaskStatus.FAILED, errors=['mimo_disabled_by_env'], provider='mimo', model_name=self.model_name)
        model_name = str(getattr(task, "assigned_model", "") or self.model_name).strip() or self.model_name
        if is_mimo_auto_router(model_name):
            return self.result(task, 'MIMO returned no usable text', TaskStatus.FAILED, errors=['mimo-auto is not a direct Xiaomi API model; use xiaomi/mimo-v2.5-pro or another xiaomi/mimo-* model'], provider='mimo', model_name=model_name)
        if not is_native_mimo_model(model_name):
            return self.result(task, 'MIMO direct mode only supports native Xiaomi models', TaskStatus.FAILED, errors=[f'unsupported_native_mimo_model:{model_name}'], provider='mimo', model_name=model_name)

        prompt_parts = [f"TASK TYPE: {task.type.value}", f"OBJECTIVE: {task.input.description}"]
        if task.input.files:
            prompt_parts.append(f"FILES: {', '.join(task.input.files)}")
        if task.input.constraints:
            prompt_parts.append(f"CONSTRAINTS: {'; '.join(task.input.constraints)}")
        if task.input.acceptance_criteria:
            prompt_parts.append(f"ACCEPTANCE CRITERIA: {'; '.join(task.input.acceptance_criteria)}")
        memory_brief = self._memory_brief(memory_context)
        if memory_brief:
            prompt_parts.append("MEMORY CONTEXT:\n" + memory_brief)
        prompt = "\n".join(prompt_parts)
        normalized_model = f"xiaomi/{normalize_mimo_model_name(model_name)}"
        self._record_execution_prompt(task, prompt, memory_context, provider='mimo', model_name=normalized_model)
        payload, error_text, status_code = invoke_mimo_native(normalized_model, prompt)
        text_output = extract_mimo_response_text(payload) if payload else ""
        if text_output:
            return self.result(task, text_output, TaskStatus.DONE, provider='mimo', model_name=normalized_model, output={"summary": text_output, "status_code": status_code, "transport": "direct_http"})
        error_parts = [part for part in [error_text, f"status_code={status_code}" if status_code is not None else None] if part]
        return self.result(task, 'MIMO returned no usable text', TaskStatus.FAILED, errors=[' | '.join(error_parts) if error_parts else 'empty_mimo_response'], provider='mimo', model_name=normalized_model, output={"status_code": status_code, "transport": "direct_http"})
