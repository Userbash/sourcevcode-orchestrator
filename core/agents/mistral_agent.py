from __future__ import annotations

import logging
import os
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

    def _select_model_for_task(self, task: Task) -> str:
        code_model = os.getenv("CODEX_MISTRAL_MODEL", "codestral-latest").strip() or "codestral-latest"
        analysis_model = os.getenv("MISTRAL_MODEL", "mistral-large-latest").strip() or "mistral-large-latest"
        fast_model = os.getenv("MISTRAL_FAST_MODEL", "mistral-medium-latest").strip() or "mistral-medium-latest"

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
        
        # Enriched prompt with context
        prompt_parts = [f"OBJECTIVE: {safe_context.get('description', '')}"]
        if task.input.files:
            prompt_parts.append(f"FILES: {', '.join(task.input.files)}")
        if task.input.constraints:
            prompt_parts.append(f"CONSTRAINTS: {'; '.join(task.input.constraints)}")
        if task.input.acceptance_criteria:
            prompt_parts.append(f"ACCEPTANCE CRITERIA: {'; '.join(task.input.acceptance_criteria)}")
        memory_brief = self._memory_brief(memory_context)
        if memory_brief:
            prompt_parts.append(f"MEMORY CONTEXT:\n{memory_brief}")
        
        prompt_content = "\n".join(prompt_parts)
        
        # Prevent 'too many arguments' by truncating description
        if len(prompt_content) > 12000:
            prompt_content = prompt_content[:12000] + "... [TRUNCATED]"
            
        max_retries = 3
        last_exc = None
        model_name = str(getattr(task, 'assigned_model', '') or self._select_model_for_task(task)).strip()
        self._record_execution_prompt(task, prompt_content, memory_context, provider=self.provider, model_name=model_name)
        
        for attempt in range(max_retries):
            try:
                # Suppress noisy httpx logging for mistral
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
                if response.status_code == 429:
                    # Exponential backoff for 429
                    wait_time = (2 ** attempt) + 1
                    logger.debug(f"Mistral 429 detected. Retrying in {wait_time}s... (Attempt {attempt+1}/{max_retries})")
                    import time
                    time.sleep(wait_time)
                    continue
                elif response.status_code in {401, 403}:
                    return self.result(task, "Mistral API error: 401 Unauthorized", TaskStatus.FAILED, 0.0, ["MISTRAL_API_KEY is invalid or missing permissions."])
                    
                response.raise_for_status()
                return self.normalize_result(response.json(), task)
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    continue
                break
        
        self.last_error = str(last_exc)
        return self.result(task, "Mistral API error", TaskStatus.FAILED, 0.0, [str(last_exc)])

    def redact_context(self, task: Task) -> dict:
        return self.security.safe_context_for_external_ai(
            {
                "description": task.input.description,
                "acceptance_criteria": task.input.acceptance_criteria,
            }
        )

    def normalize_result(self, response: dict, task: Task) -> AgentResult:
        content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            return self.result(task, "Empty response", TaskStatus.FAILED, 0.0, ["Model returned empty content"])
        result = self.result(task, content, TaskStatus.DONE, 0.85, [])
        result.provider = self.provider
        result.model_name = str(getattr(task, 'assigned_model', '') or self._select_model_for_task(task)).strip()
        return result
