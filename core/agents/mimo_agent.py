from __future__ import annotations

import json
import shutil
import subprocess

from core.agents.base_agent import BaseAgent
from core.core.models import Task, TaskStatus


class MimoAgent(BaseAgent):
    def __init__(self, agent_id: str = "mimo-router-1", default_model: str = "mimo/mimo-auto") -> None:
        super().__init__(agent_id, ["code", "fix", "test", "review", "docs", "research", "plan", "analysis", "summarization"] )
        self._provider = "mimo"
        self._model = default_model

    def _cli_path(self) -> str | None:
        return shutil.which("mimo")

    @staticmethod
    def _extract_text(stdout: str) -> tuple[str, str | None]:
        parts: list[str] = []
        error_text: str | None = None
        for line in str(stdout or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "error":
                payload = event.get("error") or {}
                data = payload.get("data") or {}
                error_text = str(data.get("message") or payload.get("name") or "mimo_run_failed")
            if isinstance(event, dict) and event.get("type") == "text":
                part = event.get("part") or {}
                text = str(part.get("text") or "").strip()
                if text:
                    parts.append(text)
        return " ".join(parts).strip(), error_text

    def run(self, task: Task, memory_context: dict | None = None):
        cli = self._cli_path()
        if not cli:
            return self.result(task, "MIMO CLI is not installed", TaskStatus.FAILED, errors=["mimo_cli_missing"], provider="mimo", model_name=self._model)

        model_name = str(getattr(task, "assigned_model", "") or self._model).strip() or self._model
        prompt_parts = [
            f"TASK TYPE: {task.type.value}",
            f"OBJECTIVE: {task.input.description}",
        ]
        if task.input.files:
            prompt_parts.append(f"FILES: {', ' .join(task.input.files)}")
        if task.input.constraints:
            prompt_parts.append(f"CONSTRAINTS: {'; ' .join(task.input.constraints)}")
        if task.input.acceptance_criteria:
            prompt_parts.append(f"ACCEPTANCE CRITERIA: {'; ' .join(task.input.acceptance_criteria)}")
        memory_brief = self._memory_brief(memory_context)
        if memory_brief:
            prompt_parts.append(f"MEMORY CONTEXT:\n{memory_brief}")
        prompt = "\n".join(prompt_parts)
        self._record_execution_prompt(task, prompt, memory_context, provider="mimo", model_name=model_name)

        try:
            proc = subprocess.run(
                ["timeout", "45s", cli, "run", "-m", model_name, "--format", "json", prompt],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            return self.result(task, "MIMO execution failed", TaskStatus.FAILED, errors=[str(exc)], provider="mimo", model_name=model_name)

        text_output, error_text = self._extract_text(proc.stdout)
        if text_output:
            return self.result(
                task,
                text_output,
                TaskStatus.DONE,
                provider="mimo",
                model_name=model_name,
                output={"summary": text_output, "stderr": (proc.stderr or "").strip(), "exit_code": proc.returncode},
            )
        reason = error_text or (proc.stderr or proc.stdout or ("timeout" if proc.returncode == 124 else "empty_mimo_response")).strip()
        return self.result(task, "MIMO returned no usable text", TaskStatus.FAILED, errors=[reason], provider="mimo", model_name=model_name)
