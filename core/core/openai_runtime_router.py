from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Complexity, Task, TaskType
from .openai_model_registry import OpenAIModelRegistry
from .model_routing_policy import ModelRoutingPolicy


class OpenAIModelUnavailableError(RuntimeError):
    pass


_NON_CHAT_MODEL_MARKERS = (
    "embedding",
    "moderation",
    "tts",
    "whisper",
    "image",
    "sora",
    "dall",
    "realtime",
    "audio",
    "transcribe",
    "speech",
)

_RUNTIME_INCOMPATIBLE_ERROR_MARKERS = (
    "no eligible resources",
    "not supported when using codex with a chatgpt account",
    "model is not supported",
    "unsupported model",
    "invalid model",
    "does not exist",
)


@dataclass(slots=True)
class OpenAIRoutingPlan:
    models: list[str]
    estimated_tokens: int
    remaining_tokens: int
    complexity: Complexity
    reason: str
    estimated_cost_usd: float = 0.0
    cost_rows: list[dict[str, Any]] | None = None


class OpenAIRuntimeRouter:
    _session_token_usage: dict[str, int] = {}
    _session_blocked_models: dict[str, set[str]] = {}

    def __init__(self) -> None:
        self.session_budget = self._read_int("OPENAI_SESSION_TOKEN_BUDGET", 120_000)
        self.registry = OpenAIModelRegistry()

    @staticmethod
    def enabled() -> bool:
        return os.getenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "true").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _read_int(key: str, default: int) -> int:
        raw = os.getenv(key, str(default)).strip()
        try:
            return int(raw)
        except ValueError:
            return default

    @staticmethod
    def _estimate_prompt_tokens(prompt: str) -> int:
        return max(8, len(prompt) // 4)

    @staticmethod
    def _estimate_completion_tokens(complexity: Complexity) -> int:
        if complexity == Complexity.LOW:
            return 512
        if complexity == Complexity.MEDIUM:
            return 1536
        if complexity == Complexity.HIGH:
            return 4096
        return 8192

    @staticmethod
    def _env_models(key: str) -> list[str]:
        raw = os.getenv(key, "").strip()
        if not raw:
            return []
        return [
            item.strip()
            for item in raw.split(",")
            if item.strip() and OpenAIRuntimeRouter.is_chat_routable_model(item.strip())
        ]

    @staticmethod
    def is_chat_routable_model(model_name: str) -> bool:
        lowered = str(model_name or "").strip().lower()
        if not lowered:
            return False
        return not any(marker in lowered for marker in _NON_CHAT_MODEL_MARKERS)

    @staticmethod
    def _runtime_inventory_path() -> Path:
        return Path(os.getenv("OPENAI_RUNTIME_INVENTORY_PATH", "core/.cache/openai_runtime_inventory.json"))

    @classmethod
    def _load_routable_allowlist(cls) -> set[str] | None:
        path = cls._runtime_inventory_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None

        fully_routable = payload.get("fully_routable_models")
        if isinstance(fully_routable, list) and fully_routable:
            return {str(item).strip() for item in fully_routable if str(item).strip()}

        validated_rows = payload.get("validated_models")
        if not isinstance(validated_rows, list) or not validated_rows:
            return None

        allowed: set[str] = set()
        for row in validated_rows:
            if not isinstance(row, dict):
                continue
            model_name = str(row.get("model") or "").strip()
            chat_ok = bool(((row.get("chat_completions") or {}).get("ok")))
            responses_ok = bool(((row.get("responses") or {}).get("ok")))
            if model_name and chat_ok and responses_ok:
                allowed.add(model_name)
        return allowed or set()

    @classmethod
    def _load_runtime_blocklist(cls) -> set[str]:
        path = cls._runtime_inventory_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        if not isinstance(payload, dict):
            return set()

        blocked: set[str] = set()
        validated_rows = payload.get("validated_models")
        if not isinstance(validated_rows, list):
            return blocked

        for row in validated_rows:
            if not isinstance(row, dict):
                continue
            model_name = str(row.get("model") or "").strip()
            if not model_name:
                continue
            if not cls.is_chat_routable_model(model_name):
                blocked.add(model_name)
                continue
            for endpoint_name in ("chat_completions", "responses"):
                endpoint_result = row.get(endpoint_name)
                if not isinstance(endpoint_result, dict):
                    continue
                error_text = str(endpoint_result.get("error") or endpoint_result.get("response_sample") or "").strip().lower()
                if any(marker in error_text for marker in _RUNTIME_INCOMPATIBLE_ERROR_MARKERS):
                    blocked.add(model_name)
                    break
        return blocked

    @classmethod
    def _filter_models_by_runtime_inventory(cls, models: list[str]) -> list[str]:
        filtered = [model for model in models if cls.is_chat_routable_model(model)]
        blocked = cls._load_runtime_blocklist()
        if blocked:
            filtered = [model for model in filtered if model not in blocked]
        require_routable = os.getenv("AI_BRIDGE_OPENAI_REQUIRE_ROUTABLE_MODELS", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not require_routable:
            return filtered
        allowlist = cls._load_routable_allowlist()
        if allowlist is None:
            return filtered
        allowlisted = [model for model in filtered if model in allowlist]
        return ModelRoutingPolicy.filter_available(allowlisted or filtered)

    @classmethod
    def is_runtime_routable_model(cls, model_name: str, *, require_allowlist: bool = True) -> bool:
        model = str(model_name or "").strip()
        if not model or not cls.is_chat_routable_model(model):
            return False
        blocked = cls._load_runtime_blocklist()
        if model in blocked:
            return False
        require_routable = os.getenv("AI_BRIDGE_OPENAI_REQUIRE_ROUTABLE_MODELS", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not require_routable or not require_allowlist:
            return True
        allowlist = cls._load_routable_allowlist()
        if allowlist is None:
            return True
        return model in allowlist

    @classmethod
    def sanitize_model(cls, model_name: str | None, *, require_allowlist: bool = True) -> str | None:
        model = str(model_name or "").strip()
        if not model:
            return None
        return model if cls.is_runtime_routable_model(model, require_allowlist=require_allowlist) else None

    @staticmethod
    def _fallbacks(complexity: Complexity, task: Task) -> list[str]:
        codex_task = task.type in {TaskType.CODE, TaskType.FIX, TaskType.TEST}
        if complexity == Complexity.LOW:
            return ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4"]
        if complexity == Complexity.MEDIUM:
            return ["gpt-5.4-mini", "gpt-5.4", "gpt-5.5", "gpt-5.4-nano"]
        if complexity == Complexity.HIGH:
            return ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"] if codex_task else ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"]
        return ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"]

    def _budget_for_task(self, task: Task) -> int:
        hints = getattr(task, "routing_hints", {}) or {}
        source = str(hints.get("source") or "").strip().lower()
        cost_tier = str(hints.get("cost_tier") or "").strip().lower()
        if source != "websocket":
            return self.session_budget
        if cost_tier == "economy":
            return self._read_int("OPENAI_SESSION_TOKEN_BUDGET_WS_ECONOMY", self._read_int("OPENAI_SESSION_TOKEN_BUDGET_WS", 40_000))
        if cost_tier == "premium":
            return self._read_int("OPENAI_SESSION_TOKEN_BUDGET_WS_PREMIUM", self._read_int("OPENAI_SESSION_TOKEN_BUDGET_WS", self.session_budget))
        return self._read_int("OPENAI_SESSION_TOKEN_BUDGET_WS", min(self.session_budget, 80_000))

    @staticmethod
    def _prioritize_for_ws_cost_tier(task: Task, models: list[str], complexity: Complexity) -> tuple[list[str], str | None]:
        hints = getattr(task, "routing_hints", {}) or {}
        source = str(hints.get("source") or "").strip().lower()
        cost_tier = str(hints.get("cost_tier") or "").strip().lower()
        if source != "websocket":
            return models, None
        if cost_tier == "economy":
            preferred = ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4", "gpt-5.5"]
            return OpenAIRuntimeRouter._dedupe(preferred + models), "ws_economy"
        if cost_tier == "premium" or complexity == Complexity.CRITICAL:
            preferred = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"]
            return OpenAIRuntimeRouter._dedupe(preferred + models), "ws_premium"
        preferred = ["gpt-5.4-mini", "gpt-5.4", "gpt-5.5", "gpt-5.4-nano"]
        return OpenAIRuntimeRouter._dedupe(preferred + models), "ws_interactive"

    def _complexity_ordered_models(self, task: Task, complexity: Complexity, *, force_refresh: bool = False) -> tuple[list[str], str, list[str]]:
        catalog = self.registry.get_catalog(force_refresh=force_refresh)
        codex_task = task.type in {TaskType.CODE, TaskType.FIX, TaskType.TEST}
        if complexity == Complexity.LOW:
            live = catalog.nano + catalog.mini + catalog.standard
            env_key = "OPENAI_LOW_MODELS"
            reason = "low_cost"
        elif complexity == Complexity.MEDIUM:
            live = catalog.mini + catalog.standard + catalog.nano
            env_key = "OPENAI_MEDIUM_MODELS"
            reason = "balanced_cost"
        elif complexity == Complexity.HIGH:
            live = (catalog.codex + catalog.standard + catalog.pro + catalog.reasoning) if codex_task else (catalog.standard + catalog.pro + catalog.reasoning + catalog.codex)
            env_key = "OPENAI_HIGH_MODELS"
            reason = "high_reasoning"
        else:
            live = (catalog.codex + catalog.pro + catalog.reasoning + catalog.standard) if codex_task else (catalog.pro + catalog.reasoning + catalog.standard + catalog.codex)
            env_key = "OPENAI_CRITICAL_MODELS"
            reason = "critical_quality"

        env_models = self._env_models(env_key)
        models = [*env_models, *live, *self._fallbacks(complexity, task), *self._env_models("OPENAI_EXTRA_MODELS")]
        ordered = self._dedupe(models)
        return self._filter_models_by_runtime_inventory(ordered), reason, env_models

    @staticmethod
    def _dedupe(models: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for model in models:
            if not OpenAIRuntimeRouter.is_chat_routable_model(model):
                continue
            if model in seen:
                continue
            seen.add(model)
            deduped.append(model)
        return deduped

    def build_plan(self, task: Task, prompt: str = "") -> OpenAIRoutingPlan:
        complexity = task.complexity or Complexity.MEDIUM
        estimated = self._estimate_prompt_tokens(prompt or task.input.description) + self._estimate_completion_tokens(complexity)
        session_id = task.session_id or "default"
        used = self._session_token_usage.get(session_id, 0)
        budget = self._budget_for_task(task)
        remaining = max(0, budget - used)
        env_models: list[str] = []

        if remaining <= 0:
            models = ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4"]
            reason = "budget_depleted_lightweight_only"
        else:
            first_call = used <= 0
            models, reason, env_models = self._complexity_ordered_models(task, complexity, force_refresh=first_call)
            models, ws_reason = self._prioritize_for_ws_cost_tier(task, models, complexity)
            if ws_reason:
                reason = ws_reason
            if estimated > remaining:
                lightweight = ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4", "gpt-5.5"]
                models = self._dedupe(lightweight + models)
                reason = "budget_guard_lightweight"

        blocked = self._session_blocked_models.get(session_id, set())
        models = [model for model in models if model not in blocked and ModelRoutingPolicy.is_model_available(model)]
        if not any(model in models for model in env_models):
            models = ModelRoutingPolicy.sort_for_task(task, models, estimated_tokens=estimated, budget_pressure="high" if estimated > remaining else "normal")
        if not models:
            raise OpenAIModelUnavailableError("no OpenAI models available for runtime routing")
        base_cost = float(os.getenv("AI_BRIDGE_OPENAI_COST_PER_1M_TOKENS_USD", "1.0"))
        cost_rows = [
            {"model": model, "multiplier": ModelRoutingPolicy.multiplier(model), "estimated_cost_usd": ModelRoutingPolicy.estimate_cost_usd(estimated, model, base_cost_per_million_tokens=base_cost)}
            for model in models
        ]
        estimated_cost = round(sum(float(row["estimated_cost_usd"]) for row in cost_rows), 6)
        return OpenAIRoutingPlan(models, estimated, remaining, complexity, reason, estimated_cost, cost_rows)

    def select_model(self, task: Task, prompt: str = "") -> str:
        return self.build_plan(task, prompt).models[0]

    def register_usage(self, task: Task, consumed_tokens: int) -> None:
        session_id = task.session_id or "default"
        current = self._session_token_usage.get(session_id, 0)
        self._session_token_usage[session_id] = max(0, current + max(0, consumed_tokens))

    def block_model(self, task: Task, model: str, *, reason: str = "") -> None:
        session_id = task.session_id or "default"
        blocked = self._session_blocked_models.setdefault(session_id, set())
        blocked.add(model)
        ModelRoutingPolicy.block_model(model, reason=reason or "probe_failed", cooldown_sec=300)
