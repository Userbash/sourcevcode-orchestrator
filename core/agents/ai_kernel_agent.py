from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import httpx
try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[assignment]


from .base_agent import BaseAgent
from core.core.env_loader import load_env_file
from core.core.models import AgentHealth, AgentResult, AgentStatus, Task, TaskStatus
from core.core.openai_payload_guard import EMPTY_ASSISTANT_RESPONSE_ERROR, EMPTY_PROVIDER_REQUEST_ERROR, extract_chat_completion_text, has_meaningful_request_payload


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
        self.set_identity(provider=self.provider, model_name=self.model_name)

    def _candidate_base_urls(self) -> list[str]:
        raw = self.base_url.rstrip('/')
        candidates: list[str] = []

        def _push(url: str) -> None:
            item = url.rstrip('/')
            if item and item not in candidates:
                candidates.append(item)

        _push(raw)
        parsed = urlsplit(raw)
        if parsed.scheme and parsed.netloc:
            host = parsed.hostname or ''
            netloc = parsed.netloc
            variants: list[str] = []
            if host == '127.0.0.1':
                variants.extend(['host.containers.internal', 'localhost'])
            elif host == 'localhost':
                variants.extend(['127.0.0.1', 'host.containers.internal'])
            elif host == 'host.containers.internal':
                variants.extend(['127.0.0.1', 'localhost'])
            for variant in variants:
                swapped_netloc = netloc.replace(host, variant, 1)
                _push(urlunsplit((parsed.scheme, swapped_netloc, parsed.path, parsed.query, parsed.fragment)))
        return candidates

    def health(self) -> AgentHealth:
        try:
            with httpx.Client(timeout=5.0) as client:
                last_error: str | None = None
                for base_url in self._candidate_base_urls():
                    try:
                        response = client.get(f'{base_url}/models', headers={'Authorization': f'Bearer {self.api_key}'})
                    except Exception as exc:
                        last_error = f'{exc}@{base_url}'
                        continue
                    if response.status_code == 200:
                        models_payload = response.json() if getattr(response, 'content', None) else {}
                        models = [str(item.get('id') or '').strip() for item in (models_payload.get('data') or []) if str(item.get('id') or '').strip()] if isinstance(models_payload, dict) else []
                        if not models or self.model_name in models:
                            self.base_url = base_url
                            return AgentHealth(agent_id=self.agent_id, status=AgentStatus.READY, capabilities=self.capabilities)
                        last_error = f'ai_kernel_model_missing@{base_url}'
                        continue
                    last_error = f'ai_kernel_status_{response.status_code}@{base_url}'
            status = AgentStatus.FAILED if last_error and 'refused' in last_error.lower() else AgentStatus.DEGRADED
            return AgentHealth(agent_id=self.agent_id, status=status, capabilities=self.capabilities, last_error=last_error)
        except Exception as exc:
            return AgentHealth(agent_id=self.agent_id, status=AgentStatus.FAILED, capabilities=self.capabilities, last_error=str(exc))

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
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
        if OpenAI is None:
            return self.result(task, "OpenAI SDK is not installed", TaskStatus.FAILED, 0.0, ["openai_sdk_missing"], provider=self.provider, model_name=model_name)
        if not has_meaningful_request_payload(prompt):
            self.last_error = EMPTY_PROVIDER_REQUEST_ERROR
            return self.result(task, 'AI kernel execution error', TaskStatus.FAILED, 0.0, [EMPTY_PROVIDER_REQUEST_ERROR], provider=self.provider, model_name=model_name)
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
        content = extract_chat_completion_text(response)
        if not content:
            self.last_error = EMPTY_ASSISTANT_RESPONSE_ERROR
            return self.result(task, 'Empty AI kernel response', TaskStatus.FAILED, 0.0, [EMPTY_ASSISTANT_RESPONSE_ERROR], provider=self.provider, model_name=model_name)
        return self.result(task, content, TaskStatus.DONE, 0.9, provider=self.provider, model_name=model_name)
