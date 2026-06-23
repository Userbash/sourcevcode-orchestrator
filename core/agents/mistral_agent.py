from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from .external_ai_agent import ExternalAIAgent
from core.core.env_loader import load_env_file
from core.core.models import AgentHealth, AgentResult, AgentStatus, Task, TaskStatus, TaskType

logger = logging.getLogger("mistral_agent")


class MistralAgent(ExternalAIAgent):
    provider = "mistral"

    def __init__(self, agent_id: str, security_manager: Any) -> None:
        super().__init__(
            agent_id,
            "https://api.mistral.ai/v1",
            ["code", "fix", "test", "review", "docs", "research", "analysis", "summarization"],
            security=security_manager,
        )
        load_env_file()
        load_env_file(".env.bridge", override=True)
        load_env_file(".env.gemini.local", override=True)
        self.api_key = os.getenv("MISTRAL_API_KEY")

    def _model_preferences(self) -> tuple[str, str, str]:
        code_model = os.getenv("CODEX_MISTRAL_MODEL", "codestral-latest").strip() or "codestral-latest"
        analysis_model = os.getenv("MISTRAL_MODEL", "mistral-large-latest").strip() or "mistral-large-latest"
        fast_model = os.getenv("MISTRAL_FAST_MODEL", "mistral-medium-latest").strip() or "mistral-medium-latest"
        return code_model, analysis_model, fast_model

    def _select_model_for_task(self, task: Task) -> str:
        code_model, analysis_model, fast_model = self._model_preferences()

        if task.type in {TaskType.CODE, TaskType.FIX}:
            return code_model
        if task.type in {TaskType.REVIEW, TaskType.RESEARCH, TaskType.TEST}:
            return analysis_model
        if task.type == TaskType.DOCS:
            complexity = getattr(task, "complexity", None)
            if complexity is not None and str(complexity.value).lower() == "low":
                return fast_model
            return analysis_model
        if task.type == TaskType.PLAN:
            return analysis_model
        return fast_model

    def _candidate_models_for_task(self, task: Task) -> list[str]:
        code_model, analysis_model, fast_model = self._model_preferences()
        preferred = str(getattr(task, "assigned_model", "") or "").strip()
        candidates: list[str] = []

        def push(model_name: str) -> None:
            model = str(model_name or "").strip()
            if model and model not in candidates:
                candidates.append(model)

        push(preferred)
        push(self._select_model_for_task(task))

        if task.type in {TaskType.CODE, TaskType.FIX}:
            push(analysis_model)
            push(fast_model)
        elif task.type in {TaskType.REVIEW, TaskType.RESEARCH, TaskType.TEST, TaskType.PLAN, TaskType.DOCS}:
            push(fast_model)
            push(code_model)
        else:
            push(analysis_model)
            push(code_model)

        return candidates

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {408, 409, 425, 429, 500, 502, 503, 504}

    def health(self) -> AgentHealth:
        if not self.api_key:
            return AgentHealth(
                agent_id=self.agent_id,
                status=AgentStatus.FAILED,
                capabilities=self.capabilities,
                last_error="auth_missing",
            )
        return AgentHealth(
            agent_id=self.agent_id, status=AgentStatus.READY, capabilities=self.capabilities
        )

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        if not self.api_key:
            return self.result(task, "Auth missing", TaskStatus.FAILED, 0.0, ["MISTRAL_API_KEY not set"])

        safe_context = self.redact_context(task)

        prompt_parts = [f"OBJECTIVE: {safe_context.get('description', '')}"]
        if task.input.files:
            prompt_parts.append(f"FILES: {', '.join(task.input.files)}")
        if task.input.constraints:
            prompt_parts.append(f"CONSTRAINTS: {'; '.join(task.input.constraints)}")
        if task.input.acceptance_criteria:
            prompt_parts.append(f"ACCEPTANCE CRITERIA: {'; '.join(task.input.acceptance_criteria)}")
        memory_brief = self._memory_brief(memory_context)
        if memory_brief:
            prompt_parts.append("MEMORY CONTEXT:\n" + memory_brief)

        prompt_content = "\n".join(prompt_parts)
        if len(prompt_content) > 12000:
            prompt_content = prompt_content[:12000] + "... [TRUNCATED]"

        max_retries = 3
        last_exc = None
        candidate_models = self._candidate_models_for_task(task)
        self._record_execution_prompt(task, prompt_content, memory_context, provider=self.provider, model_name=candidate_models[0])

        for model_name in candidate_models:
            for attempt in range(max_retries):
                try:
                    logging.getLogger("httpx").setLevel(logging.WARNING)

                    response = httpx.post(
                        f"{self.endpoint}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                        json={
                            "model": model_name,
                            "messages": [{"role": "user", "content": prompt_content}],
                        },
                        timeout=45.0,
                    )
                    if response.status_code in {401, 403}:
                        return self.result(task, "Mistral API error: 401 Unauthorized", TaskStatus.FAILED, 0.0, ["MISTRAL_API_KEY is invalid or missing permissions."], provider=self.provider, model_name=model_name)
                    if self._is_retryable_status(response.status_code):
                        wait_time = (2 ** attempt) + 1
                        if attempt < max_retries - 1:
                            logger.debug("Mistral transient status %s on model %s. Retrying in %ss... (%s/%s)", response.status_code, model_name, wait_time, attempt + 1, max_retries)
                            time.sleep(wait_time)
                            continue
                    response.raise_for_status()
                    return self.normalize_result(response.json(), task, model_name=model_name)
                except Exception as exc:
                    last_exc = exc
                    if attempt < max_retries - 1:
                        continue
                    break

        self.last_error = str(last_exc)
        failed_model = candidate_models[-1] if candidate_models else self._select_model_for_task(task)
        return self.result(task, "Mistral API error", TaskStatus.FAILED, 0.0, [str(last_exc)], provider=self.provider, model_name=failed_model)

    def redact_context(self, task: Task) -> dict:
        return self.security.safe_context_for_external_ai(
            {
                "description": task.input.description,
                "acceptance_criteria": task.input.acceptance_criteria,
            }
        )

    def normalize_result(self, response: dict, task: Task, *, model_name: str | None = None) -> AgentResult:
        content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            resolved_model = model_name or str(getattr(task, 'assigned_model', '') or self._select_model_for_task(task)).strip()
            return self.result(task, "Empty response", TaskStatus.FAILED, 0.0, ["Model returned empty content"], provider=self.provider, model_name=resolved_model)
        result = self.result(task, content, TaskStatus.DONE, 0.85, [])
        result.provider = self.provider
        result.model_name = str(model_name or getattr(task, 'assigned_model', '') or self._select_model_for_task(task)).strip()
        return result
