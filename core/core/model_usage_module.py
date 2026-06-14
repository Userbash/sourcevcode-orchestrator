from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .kernel_api import KernelAPI
from .models import AgentResult, Task

logger = logging.getLogger("model_usage_module")

RATE_CARD_USD_PER_1K: dict[str, tuple[float, float]] = {
    "codestral-latest": (0.0003, 0.0009),
    "mistral-medium-latest": (0.0004, 0.0012),
    "mistral-large-latest": (0.0020, 0.0060),
    "devstral-latest": (0.0012, 0.0035),
}

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

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        raw = os.environ.get(name, str(default)).strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            return default

    def _estimate_local_usage_cost(self, *, model: str, input_tokens: int, output_tokens: int, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
        runtime = dict(runtime or {})
        latency_sec = float(runtime.get("latency_sec") or runtime.get("wall_time_sec") or 0.0)
        if latency_sec <= 0.0:
            prompt_eval = int(runtime.get("prompt_eval_count") or 0)
            eval_count = int(runtime.get("eval_count") or 0)
            token_total = max(1, prompt_eval + eval_count, input_tokens + output_tokens)
            throughput = self._env_float("AI_BRIDGE_LOCAL_LLM_TOKENS_PER_SEC", 35.0)
            latency_sec = token_total / max(1.0, throughput)

        gpu_time_sec = float(runtime.get("gpu_time_sec") or runtime.get("eval_duration_sec") or latency_sec)
        cpu_time_sec = float(runtime.get("cpu_time_sec") or max(latency_sec, gpu_time_sec * self._env_float("AI_BRIDGE_LOCAL_LLM_CPU_TIME_MULTIPLIER", 0.35)))
        ram_gb = float(runtime.get("ram_gb") or self._env_float("AI_BRIDGE_LOCAL_LLM_RAM_GB", 8.0))
        gpu_watts = float(runtime.get("gpu_watts") or self._env_float("AI_BRIDGE_LOCAL_LLM_GPU_WATTS", 220.0))
        cpu_watts = float(runtime.get("cpu_watts") or self._env_float("AI_BRIDGE_LOCAL_LLM_CPU_WATTS", 45.0))
        ram_watts_per_gb = float(runtime.get("ram_watts_per_gb") or self._env_float("AI_BRIDGE_LOCAL_LLM_RAM_WATTS_PER_GB", 0.38))
        electricity_usd_per_kwh = self._env_float("AI_BRIDGE_LOCAL_LLM_ELECTRICITY_USD_PER_KWH", 0.12)
        amortized_hardware_usd_per_hour = self._env_float("AI_BRIDGE_LOCAL_LLM_AMORTIZED_USD_PER_HOUR", 0.55)
        operations_overhead_usd_per_hour = self._env_float("AI_BRIDGE_LOCAL_LLM_OPERATIONS_USD_PER_HOUR", 0.08)
        request_overhead_usd = self._env_float("AI_BRIDGE_LOCAL_LLM_REQUEST_OVERHEAD_USD", 0.0002)

        energy_kwh = ((gpu_watts * gpu_time_sec) + (cpu_watts * cpu_time_sec) + (ram_gb * ram_watts_per_gb * latency_sec)) / 3_600_000.0
        energy_cost = energy_kwh * electricity_usd_per_kwh
        amortized_cost = (latency_sec / 3600.0) * amortized_hardware_usd_per_hour
        operations_cost = (latency_sec / 3600.0) * operations_overhead_usd_per_hour
        memory_pressure_cost = (ram_gb * latency_sec / 3600.0) * self._env_float("AI_BRIDGE_LOCAL_LLM_RAM_PRESSURE_USD_PER_GB_HOUR", 0.006)
        estimated_cost = round(request_overhead_usd + energy_cost + amortized_cost + operations_cost + memory_pressure_cost, 6)

        return {
            "provider": "local",
            "model": str(model or "local"),
            "currency": "USD",
            "input_tokens": max(0, int(input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
            "estimated_cost_usd": estimated_cost,
            "cost_components": {
                "request_overhead_usd": round(request_overhead_usd, 6),
                "energy_usd": round(energy_cost, 6),
                "amortized_hardware_usd": round(amortized_cost, 6),
                "operations_usd": round(operations_cost, 6),
                "memory_pressure_usd": round(memory_pressure_cost, 6),
                "energy_kwh": round(energy_kwh, 8),
                "latency_sec": round(latency_sec, 6),
                "gpu_time_sec": round(gpu_time_sec, 6),
                "cpu_time_sec": round(cpu_time_sec, 6),
                "ram_gb": round(ram_gb, 3),
            },
        }

    @staticmethod
    def _normalize_provider(provider: str, model: str) -> str:
        raw_provider = str(provider or "").strip().lower()
        raw_model = str(model or "").strip().lower()
        if raw_provider:
            if raw_provider in {"google", "gemini", "agy", "antigravity-cli"}:
                return "antigravity"
            if raw_provider in {"local", "local_llm", "ollama"}:
                return "local"
            return raw_provider
        if "mistral" in raw_model or "codestral" in raw_model or "devstral" in raw_model:
            return "mistral"
        if raw_model.startswith("gpt") or "openai" in raw_model:
            return "openai"
        return "unknown"

    def estimate_usage_cost(self, model: str, *, input_tokens: int, output_tokens: int, provider: str = "", runtime: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_model = str(model or "unknown").strip()
        normalized_provider = self._normalize_provider(provider, normalized_model)
        if normalized_provider == "local":
            return self._estimate_local_usage_cost(model=normalized_model, input_tokens=input_tokens, output_tokens=output_tokens, runtime=runtime)
        input_rate, output_rate = RATE_CARD_USD_PER_1K.get(normalized_model, (0.0, 0.0))
        estimated_cost = round(((max(0, int(input_tokens)) / 1000.0) * input_rate) + ((max(0, int(output_tokens)) / 1000.0) * output_rate), 6)
        return {
            "provider": normalized_provider,
            "model": normalized_model,
            "currency": "USD",
            "input_tokens": max(0, int(input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
            "estimated_cost_usd": estimated_cost,
            "cost_components": {},
        }

    def before_task(self, task: Task, context: dict[str, Any]) -> None:
        model = context.get("model") or context.get("selected_model") or "unknown"
        provider = self._normalize_provider(
            str(context.get("provider") or context.get("selected_provider") or ""),
            str(model),
        )
        
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
        provider = self._normalize_provider(
            str(context.get("provider") or context.get("selected_provider") or result.provider or ""),
            str(model),
        )
        
        # Simulate token extraction. In a real scenario, this would come from result.metadata 
        # or the LLM provider API response (e.g. usage.total_tokens)
        # We will estimate tokens if not explicitly provided: ~ 4 chars per token.
        input_len = len(str(task.input))
        output_len = len(str(result.output))
        estimated_input_tokens = max(1, input_len // 4)
        estimated_output_tokens = max(1, output_len // 4)
        estimated_tokens = estimated_input_tokens + estimated_output_tokens
        
        # Override with actual tokens if provider sent them
        actual_tokens = context.get("usage_tokens", estimated_tokens)
        if isinstance(actual_tokens, int):
            actual_input_tokens = min(actual_tokens, estimated_input_tokens)
            actual_output_tokens = max(0, actual_tokens - actual_input_tokens)
        else:
            actual_input_tokens = estimated_input_tokens
            actual_output_tokens = estimated_output_tokens
            actual_tokens = estimated_tokens

        runtime = context.get("usage_runtime")
        if not isinstance(runtime, dict):
            runtime = result.output.get("local_usage") if hasattr(result.output, "get") else None
        cost_estimate = self.estimate_usage_cost(
            str(model),
            input_tokens=actual_input_tokens,
            output_tokens=actual_output_tokens,
            provider=provider,
            runtime=runtime if isinstance(runtime, dict) else None,
        )

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
            "estimated_cost_usd": cost_estimate["estimated_cost_usd"],
            "currency": cost_estimate["currency"],
            "cost_components": dict(cost_estimate.get("cost_components") or {}),
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
