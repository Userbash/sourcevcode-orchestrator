from __future__ import annotations

import os

import httpx
from openai import OpenAI

from .base_agent import BaseAgent
from core.core.env_loader import load_env_file
from core.core.models import AgentHealth, AgentStatus, Task, TaskStatus


class AIKernelAgent(BaseAgent):
    provider = "ai_kernel"

    def __init__(self, agent_id: str = "ai-kernel-qwen36-1") -> None:
        super().__init__(agent_id, ["code", "fix", "test", "review", "docs", "research"])
        load_env_file()
        load_env_file('.env.bridge', override=True)
        self.base_url = (os.getenv('AI_KERNEL_BASE_URL') or 'http://127.0.0.1:8012/v1').rstrip('/')
        self.api_key = (os.getenv('AI_KERNEL_API_KEY') or 'local').strip()
        self.model_name = (os.getenv('AI_KERNEL_MODEL_ALIAS') or 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m').strip()
        self.timeout_sec = float(os.getenv('AI_KERNEL_TIMEOUT_SEC', '120'))
        self._provider = self.provider
        self._model = self.model_name

    def health(self) -> AgentHealth:
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f'{self.base_url}/models', headers={'Authorization': f'Bearer {self.api_key}'})
            if response.status_code == 200:
                return AgentHealth(agent_id=self.agent_id, status=AgentStatus.READY, capabilities=self.capabilities)
            return AgentHealth(agent_id=self.agent_id, status=AgentStatus.DEGRADED, capabilities=self.capabilities, last_error=f'ai_kernel_status_{response.status_code}')
        except Exception as exc:
            return AgentHealth(agent_id=self.agent_id, status=AgentStatus.FAILED, capabilities=self.capabilities, last_error=str(exc))

    def run(self, task: Task, memory_context: dict | None = None):
        prompt_parts = [f'OBJECTIVE: {task.input.description}']
        if task.input.files:
            prompt_parts.append(f"FILES: {', '.join(task.input.files)}")
        if task.input.constraints:
            prompt_parts.append(f"CONSTRAINTS: {'; '.join(task.input.constraints)}")
        if task.input.acceptance_criteria:
            prompt_parts.append(f"ACCEPTANCE CRITERIA: {'; '.join(task.input.acceptance_criteria)}")
        memory_brief = self._memory_brief(memory_context)
        if memory_brief:
            prompt_parts.append("MEMORY CONTEXT:\n" + memory_brief)
        prompt = '\n'.join(prompt_parts)
        model_name = str(getattr(task, 'assigned_model', '') or self.model_name).strip()
        self._record_execution_prompt(task, prompt, memory_context, provider=self.provider, model_name=model_name)
        client = OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=1)
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[{'role': 'user', 'content': prompt}],
                temperature=0.2,
            )
        except Exception as exc:
            self.last_error = str(exc)
            return self.result(task, 'AI kernel execution error', TaskStatus.FAILED, 0.0, [str(exc)], provider=self.provider, model_name=model_name)
        content = response.choices[0].message.content or ''
        if not content.strip():
            return self.result(task, 'Empty AI kernel response', TaskStatus.FAILED, 0.0, ['empty_ai_kernel_response'], provider=self.provider, model_name=model_name)
        return self.result(task, content, TaskStatus.DONE, 0.9, provider=self.provider, model_name=model_name)
