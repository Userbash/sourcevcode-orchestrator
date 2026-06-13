from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.models import Task, TaskStatus


class LocalLLMAgent(BaseAgent):
    def __init__(self, agent_id: str = "local-llm-1", model_name: str = "qwen2.5:32b-instruct-q4_k_m") -> None:
        super().__init__(agent_id, ["plan", "docs", "research", "review", "test"])
        self._provider = "local"
        self._model = model_name

    def run(self, task: Task, memory_context: dict | None = None):
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return self.result(task, "Local LLM agent is not attached to orchestrator", TaskStatus.FAILED, errors=["orchestrator_missing"])

        local_llm = orchestrator.get_module("local_llm")
        if not local_llm or not getattr(local_llm, "ready", False):
            return self.result(task, "Local LLM module is not ready", TaskStatus.FAILED, errors=["local_llm_not_ready"])

        prompt_parts = [
            f"TASK TYPE: {task.type.value}",
            f"OBJECTIVE: {task.input.description}",
        ]
        if task.input.files:
            prompt_parts.append(f"FILES: {', '.join(task.input.files)}")
        if task.input.constraints:
            prompt_parts.append(f"CONSTRAINTS: {'; '.join(task.input.constraints)}")
        if task.input.acceptance_criteria:
            prompt_parts.append(f"ACCEPTANCE CRITERIA: {'; '.join(task.input.acceptance_criteria)}")
        if memory_context:
            prompt_parts.append(f"MEMORY CONTEXT: {memory_context}")

        system = (
            "You are the local LLM execution lane for the orchestrator. "
            "Return a concise, actionable response that helps complete the assigned planning, docs, research, review, or test task. "
            "Do not claim file edits or commands you did not perform."
        )
        response = local_llm.query("\n".join(prompt_parts), model_name=getattr(local_llm, "model_name", self._model), system=system)
        if not response:
            return self.result(task, "Local LLM returned no output", TaskStatus.FAILED, errors=["empty_local_llm_response"])
        return self.result(task, response, TaskStatus.DONE, provider="local", model_name=getattr(local_llm, "model_name", self._model))
