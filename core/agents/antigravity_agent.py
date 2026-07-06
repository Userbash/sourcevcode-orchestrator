from __future__ import annotations

import os

from .base_agent import BaseAgent
from core.core.antigravity_provider import extract_antigravity_response_text, invoke_antigravity_native
from core.core.models import AgentResult, Task, TaskStatus


class AntigravityDirectAgent(BaseAgent):
    def __init__(self, agent_id: str, model_name: str = "gemini-2.5-flash-lite") -> None:
        super().__init__(agent_id, capabilities=["code", "review", "test", "docs", "research"])
        self.model_name = model_name
        self.provider = "antigravity"

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        self.active_tasks += 1
        try:
            api_key = (os.getenv("ANTIGRAVITY_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
            if not api_key:
                self.last_error = "ANTIGRAVITY_API_KEY environment variable is required"
                return self.result(task, "Antigravity generation failed", TaskStatus.FAILED, errors=[self.last_error], provider=self.provider, model_name=self.model_name)

            prompt = task.input.description
            memory_brief = self._memory_brief(memory_context)
            if memory_brief:
                prompt = f"{prompt}\nMEMORY CONTEXT:\n{memory_brief}"
            self._record_execution_prompt(task, prompt, memory_context, provider=self.provider, model_name=self.model_name)

            payload, error_text, status_code = invoke_antigravity_native(self.model_name, prompt)
            text = extract_antigravity_response_text(payload) if payload else ""
            if text.strip():
                return self.result(task, text, TaskStatus.DONE, provider=self.provider, model_name=self.model_name)
            self.last_error = error_text or (f"status_code={status_code}" if status_code is not None else "empty_antigravity_response")
            return self.result(task, "Antigravity generation failed", TaskStatus.FAILED, errors=[self.last_error], provider=self.provider, model_name=self.model_name)
        finally:
            self.active_tasks = max(0, self.active_tasks - 1)


AntigravityAgent = AntigravityDirectAgent
