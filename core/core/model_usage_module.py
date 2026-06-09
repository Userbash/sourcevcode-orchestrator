from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .kernel_api import KernelAPI
from .models import AgentResult, Task

logger = logging.getLogger("model_usage_module")

@dataclass
class ModelStats:
    used_tokens: int = 0
    limit_tokens: int = 1000000  # Default limit per session/day
    requests_count: int = 0

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.limit_tokens - self.used_tokens)

    @property
    def used_percentage(self) -> float:
        if self.limit_tokens <= 0:
            return 100.0
        return round((self.used_tokens / self.limit_tokens) * 100, 2)

    @property
    def remaining_percentage(self) -> float:
        if self.limit_tokens <= 0:
            return 0.0
        return round((self.remaining_tokens / self.limit_tokens) * 100, 2)

    @property
    def usage_percentage(self) -> float:
        # Backward-compatible alias for callers that still expect usage_percentage.
        return self.used_percentage

@dataclass(slots=True)
class ModelUsageModule:
    name: str = "model_usage"
    _api: KernelAPI | None = None
    current: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, ModelStats] = field(default_factory=dict)
    
    # Optional: Hardcoded or configurable limits per model
    _model_limits: dict[str, int] = field(default_factory=lambda: {
        "gpt-4": 500000,
        "gpt-coding-large": 800000,
        "gemini-1.5-pro": 1000000,
        "mistral-large-latest": 1500000,
    })

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        if self._api:
            self._api.log("info", f"[{self.name.upper()}] Module loaded. Ready to track token usage.")

    def on_unload(self) -> None:
        if self._api:
            self._api.log("info", f"[{self.name.upper()}] Module unloaded. Tracking stopped.")
        self.current = None

    @staticmethod
    def _threshold_from_env(name: str, default: float) -> float:
        raw = os.environ.get(name, str(default))
        try:
            return max(0.0, min(100.0, float(raw)))
        except ValueError:
            return default

    def _budget_thresholds(self) -> tuple[float, float, float]:
        warn_below = self._threshold_from_env("AI_BRIDGE_TOKEN_WARN_BELOW_PERCENT", 20.0)
        reduce_below = self._threshold_from_env("AI_BRIDGE_TOKEN_REDUCE_BELOW_PERCENT", 10.0)
        error_below = self._threshold_from_env("AI_BRIDGE_TOKEN_ERROR_BELOW_PERCENT", 5.0)
        warn_below = max(warn_below, reduce_below, error_below)
        reduce_below = max(min(reduce_below, warn_below), error_below)
        error_below = min(error_below, reduce_below)
        return warn_below, reduce_below, error_below

    def evaluate_model_budget(self, model: str, *, planned_tokens: int = 0) -> dict[str, Any]:
        stat = self._get_or_create_stats(model)
        warn_below, reduce_below, error_below = self._budget_thresholds()
        planned = max(0, int(planned_tokens))
        remaining_after = max(0, stat.remaining_tokens - planned)
        if stat.limit_tokens <= 0:
            remaining_percentage = 0.0
        else:
            remaining_percentage = round((remaining_after / stat.limit_tokens) * 100, 2)

        action = "ok"
        if remaining_percentage <= error_below:
            action = "error"
        elif remaining_percentage <= reduce_below:
            action = "reduce"
        elif remaining_percentage <= warn_below:
            action = "warn"

        return {
            "model": model,
            "limit_tokens": stat.limit_tokens,
            "used_tokens": stat.used_tokens,
            "planned_tokens": planned,
            "remaining_tokens": remaining_after,
            "remaining_percentage": remaining_percentage,
            "used_percentage": stat.used_percentage,
            "warn_below_percentage": warn_below,
            "reduce_below_percentage": reduce_below,
            "error_below_percentage": error_below,
            "action": action,
        }

    def should_reduce_parallelism(self) -> bool:
        for model in self.stats:
            policy = self.evaluate_model_budget(model)
            if policy["action"] in {"reduce", "error"}:
                return True
        return False

    def _get_or_create_stats(self, model: str) -> ModelStats:
        if model not in self.stats:
            limit = self._model_limits.get(model, 1000000) # Default 1M
            self.stats[model] = ModelStats(limit_tokens=limit)
        return self.stats[model]

    def before_task(self, task: Task, context: dict[str, Any]) -> None:
        model = context.get("model") or context.get("selected_model") or "unknown"
        provider = context.get("provider") or context.get("selected_provider") or "unknown"
        
        self.current = {
            "task_id": task.task_id,
            "task_type": task.type.value,
            "provider": provider,
            "model": model,
            "agent_id": context.get("agent_id"),
            "started_at": datetime.now(UTC).isoformat(),
        }

    def after_task(self, task: Task, result: AgentResult, context: dict[str, Any]) -> None:
        model = context.get("model") or context.get("selected_model") or "unknown"
        provider = context.get("provider") or context.get("selected_provider") or "unknown"
        
        # Simulate token extraction. In a real scenario, this would come from result.metadata 
        # or the LLM provider API response (e.g. usage.total_tokens)
        # We will estimate tokens if not explicitly provided: ~ 4 chars per token.
        input_len = len(str(task.input))
        output_len = len(str(result.output))
        estimated_tokens = (input_len + output_len) // 4
        
        # Override with actual tokens if provider sent them
        actual_tokens = context.get("usage_tokens", estimated_tokens)
        
        # Update Stats
        model_stat = self._get_or_create_stats(model)
        model_stat.used_tokens += actual_tokens
        model_stat.requests_count += 1

        record = {
            "task_id": task.task_id,
            "task_type": task.type.value,
            "provider": provider,
            "model": model,
            "agent_id": context.get("agent_id") or result.agent_id,
            "status": result.status.value,
            "tokens_used": actual_tokens,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        self.history.append(record)
        self.current = None
        
        if self._api:
            self._api.log("info", f"[{self.name.upper()}] {model} used {actual_tokens} tokens. ({model_stat.used_percentage}% of limit)")

    def get_statistics(self) -> dict[str, Any]:
        """Exposes structured statistics for the API / CLI."""
        summary = {}
        total_used = 0
        
        for model, stat in self.stats.items():
            summary[model] = {
                "used_tokens": stat.used_tokens,
                "limit_tokens": stat.limit_tokens,
                "remaining_tokens": stat.remaining_tokens,
                "remaining_percentage": stat.remaining_percentage,
                "used_percentage": stat.used_percentage,
                "usage_percentage": stat.usage_percentage,
                "requests_count": stat.requests_count,
                "status": self.evaluate_model_budget(model)["action"],
            }
            total_used += stat.used_tokens
            
        return {
            "total_tokens_used": total_used,
            "models": summary
        }

    def finalize(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "history": self.history,
            "stats": self.get_statistics()
        }
