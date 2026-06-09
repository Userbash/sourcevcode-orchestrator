from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .models import Complexity, Task, TaskType
from .qwen_model_registry import QwenModelRegistry

@dataclass(slots=True)
class QwenRoutingPlan:
    models: list[str]
    estimated_tokens: int
    context_window: int
    complexity: Complexity
    reason: str

class QwenRuntimeRouter:
    """
    Dynamic routing and token management for Qwen models.
    """
    _usage_stats: dict[str, int] = {}

    def __init__(self) -> None:
        self.registry = QwenModelRegistry()
        self.session_budget = int(os.getenv("QWEN_SESSION_TOKEN_BUDGET", "200000"))

    def build_plan(self, task: Task, prompt: str = "") -> QwenRoutingPlan:
        catalog = self.registry.get_catalog()
        complexity = task.complexity or Complexity.MEDIUM
        
        # 1. Estimate Token Usage
        prompt_len = len(prompt or task.input.description)
        est_input = prompt_len // 4
        est_output = self._get_expected_output(complexity)
        total_est = est_input + est_output

        # 2. Strategy Selection
        if task.type in {TaskType.CODE, TaskType.TEST}:
            models = catalog.coder + catalog.max + catalog.plus
            reason = "specialized_coding"
            context_window = 32768
        elif complexity == Complexity.HIGH or complexity == Complexity.CRITICAL:
            models = catalog.max + catalog.plus + catalog.instruct
            reason = "high_reasoning"
            context_window = 32768
        else:
            models = catalog.turbo + catalog.instruct + catalog.standard
            reason = "efficient_general"
            context_window = 8192

        # 3. Handle Local Fallback
        if not models:
            models = ["qwen2.5:32b-instruct-q4_k_m"]
            reason = "local_fallback"
            context_window = 32768

        return QwenRoutingPlan(
            models=models,
            estimated_tokens=total_est,
            context_window=context_window,
            complexity=complexity,
            reason=reason
        )

    def _get_expected_output(self, complexity: Complexity) -> int:
        mapping = {
            Complexity.LOW: 512,
            Complexity.MEDIUM: 2048,
            Complexity.HIGH: 4096,
            Complexity.CRITICAL: 8192
        }
        return mapping.get(complexity, 2048)

    def register_usage(self, task_id: str, tokens: int) -> None:
        self._usage_stats[task_id] = self._usage_stats.get(task_id, 0) + tokens
