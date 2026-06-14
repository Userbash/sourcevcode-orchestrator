from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


HIGH_RISK_KEYWORDS = {"security", "auth", "rbac", "payment", "production", "migration", "destructive", "release"}
GATEWAY_TASK_TYPES = {"plan", "review", "research", "docs", "test"}
CODING_TASK_TYPES = {"code", "fix", "refactor"}
LOCAL_DIRECT_TASK_TYPES = {"docs", "plan", "research", "review"}


@dataclass(slots=True)
class MistralRateCard:
    input_per_1k: float
    output_per_1k: float


class MistralGovernance:
    def __init__(self) -> None:
        self.rate_card = {
            "mistral-medium-latest": self._rate("MISTRAL_RATE_MEDIUM_INPUT", "MISTRAL_RATE_MEDIUM_OUTPUT", 0.0004, 0.0012),
            "codestral-latest": self._rate("MISTRAL_RATE_CODE_INPUT", "MISTRAL_RATE_CODE_OUTPUT", 0.0003, 0.0009),
            "mistral-large-latest": self._rate("MISTRAL_RATE_LARGE_INPUT", "MISTRAL_RATE_LARGE_OUTPUT", 0.0020, 0.0060),
            "devstral-latest": self._rate("MISTRAL_RATE_DEVSTRAL_INPUT", "MISTRAL_RATE_DEVSTRAL_OUTPUT", 0.0012, 0.0035),
        }

    @staticmethod
    def _rate(input_key: str, output_key: str, default_input: float, default_output: float) -> MistralRateCard:
        def _read(key: str, default: float) -> float:
            raw = os.getenv(key, str(default)).strip()
            try:
                return max(0.0, float(raw))
            except ValueError:
                return default

        return MistralRateCard(
            input_per_1k=_read(input_key, default_input),
            output_per_1k=_read(output_key, default_output),
        )

    @staticmethod
    def _task_type(task: Any) -> str:
        return str(getattr(getattr(task, "type", None), "value", getattr(task, "type", "unknown"))).lower().strip() or "unknown"

    @staticmethod
    def _complexity(task: Any) -> str:
        return str(getattr(getattr(task, "complexity", None), "value", getattr(task, "complexity", "medium"))).lower().strip() or "medium"

    @staticmethod
    def _priority(task: Any) -> str:
        return str(getattr(getattr(task, "priority", None), "value", getattr(task, "priority", "normal"))).lower().strip() or "normal"

    @staticmethod
    def _task_text(task: Any) -> str:
        description = str(getattr(getattr(task, "input", None), "description", "") or "")
        files = getattr(getattr(task, "input", None), "files", []) or []
        constraints = getattr(getattr(task, "input", None), "constraints", []) or []
        criteria = getattr(getattr(task, "input", None), "acceptance_criteria", []) or []
        pieces = [description, *[str(item) for item in files], *[str(item) for item in constraints], *[str(item) for item in criteria]]
        return " ".join(piece.strip() for piece in pieces if str(piece).strip()).lower()

    @staticmethod
    def _acceptance_criteria(task: Any) -> list[str]:
        values = getattr(getattr(task, "input", None), "acceptance_criteria", []) or []
        return [str(item).strip() for item in values if str(item).strip()]

    @staticmethod
    def _files(task: Any) -> list[str]:
        values = getattr(getattr(task, "input", None), "files", []) or []
        return [str(item).strip() for item in values if str(item).strip()]

    @staticmethod
    def _is_high_risk(task_text: str, complexity: str, priority: str) -> bool:
        return complexity in {"high", "critical"} or priority == "critical" or any(token in task_text for token in HIGH_RISK_KEYWORDS)

    def preferred_model_for(self, task: Any) -> str:
        task_type = self._task_type(task)
        complexity = self._complexity(task)
        task_text = self._task_text(task)
        if task_type in CODING_TASK_TYPES:
            if "repo" in task_text or "workflow" in task_text or "agent" in task_text:
                return "devstral-latest"
            return "codestral-latest"
        if task_type in {"review", "research", "docs"}:
            return "mistral-large-latest" if complexity in {"medium", "high", "critical"} else "mistral-medium-latest"
        if task_type == "test":
            return "mistral-large-latest" if complexity in {"medium", "high", "critical"} else "mistral-medium-latest"
        if task_type == "plan":
            return "mistral-large-latest" if complexity in {"medium", "high", "critical"} else "mistral-medium-latest"
        return "mistral-medium-latest"

    def management_profile_for(self, task: Any, *, local_ready: bool, provider_ready: bool) -> str:
        task_type = self._task_type(task)
        complexity = self._complexity(task)
        task_text = self._task_text(task)
        priority = self._priority(task)
        high_risk = self._is_high_risk(task_text, complexity, priority)
        if not provider_ready:
            return "disabled"
        if task_type in CODING_TASK_TYPES:
            return "coding_supervisor"
        if task_type in GATEWAY_TASK_TYPES and local_ready and (complexity in {"medium", "high", "critical"} or high_risk):
            return "strict_gateway"
        if task_type in LOCAL_DIRECT_TASK_TYPES and local_ready and complexity == "low" and not high_risk:
            return "local_first"
        if task_type in {"review", "research"}:
            return "review_manager"
        return "direct_executor"

    def selected_owner_for(self, task: Any, *, local_ready: bool, provider_ready: bool, local_recommended_owner: str = "") -> str:
        profile = self.management_profile_for(task, local_ready=local_ready, provider_ready=provider_ready)
        task_type = self._task_type(task)
        if profile == "disabled":
            return "local_llm" if local_ready else "core"
        if profile == "local_first":
            return "local_llm"
        if profile == "strict_gateway":
            return "mistral_gateway"
        if task_type in CODING_TASK_TYPES:
            return "mistral"
        if task_type in {"review", "research"} and local_ready and local_recommended_owner == "local_llm":
            return "mistral_gateway"
        return "mistral"

    def estimate_cost(self, model_name: str, *, input_tokens: int, output_tokens: int) -> dict[str, Any]:
        card = self.rate_card.get(model_name, self.rate_card["mistral-medium-latest"])
        gateway = round((input_tokens / 1000.0) * card.input_per_1k + (output_tokens / 1000.0) * card.output_per_1k, 6)
        return {
            "currency": "USD",
            "model_name": model_name,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "gateway_usd": gateway,
            "local_worker_usd": 0.0,
            "total_usd": gateway,
        }

    def delegation_plan_for(self, task: Any, *, local_ready: bool) -> list[dict[str, Any]]:
        if not local_ready:
            return []
        task_type = self._task_type(task)
        complexity = self._complexity(task)
        criteria = self._acceptance_criteria(task)
        files = self._files(task)
        plan: list[dict[str, Any]] = []
        for item in criteria:
            lowered = item.lower()
            delegated_type = "plan"
            if "test" in lowered:
                delegated_type = "test"
            elif "doc" in lowered or "summary" in lowered:
                delegated_type = "docs"
            elif "compare" in lowered or "research" in lowered:
                delegated_type = "research"
            plan.append({
                "delegate_to": "local_llm",
                "task_type": delegated_type,
                "objective": item,
                "complexity": "low" if complexity == "medium" else complexity,
                "mode": "subtask",
            })
        if files:
            for file_path in files[:2]:
                plan.append({
                    "delegate_to": "local_llm",
                    "task_type": "docs" if task_type == "docs" else "research",
                    "objective": f"Summarize impact for {file_path}",
                    "complexity": "low",
                    "mode": "file_scope",
                })
        if not plan and task_type in GATEWAY_TASK_TYPES:
            plan = [
                {
                    "delegate_to": "local_llm",
                    "task_type": "plan",
                    "objective": "Break the request into simple execution steps",
                    "complexity": "low",
                    "mode": "fallback",
                }
            ]
        return plan

    def build_profile(self, task: Any, *, local_advisory: dict[str, Any] | None = None, current_budget: float = 0.0, provider_ready: bool = True) -> dict[str, Any]:
        local_advisory = local_advisory or {}
        local_ready = bool(local_advisory.get("ready"))
        local_owner = str(local_advisory.get("recommended_owner") or "").strip().lower()
        task_type = self._task_type(task)
        complexity = self._complexity(task)
        priority = self._priority(task)
        task_text = self._task_text(task)
        high_risk = self._is_high_risk(task_text, complexity, priority)
        preferred_model = self.preferred_model_for(task)
        management_profile = self.management_profile_for(task, local_ready=local_ready, provider_ready=provider_ready)
        selected_owner = self.selected_owner_for(task, local_ready=local_ready, provider_ready=provider_ready, local_recommended_owner=local_owner)
        delegation_plan = self.delegation_plan_for(task, local_ready=local_ready) if selected_owner == "mistral_gateway" else []
        authority_tier = "L1_worker"
        if selected_owner == "mistral_gateway":
            authority_tier = "L3_gateway" if high_risk or complexity in {"high", "critical"} else "L2_gateway"
        elif selected_owner == "mistral":
            authority_tier = "L2_executor"
        elif selected_owner == "local_llm":
            authority_tier = "L0_local"

        input_tokens = max(128, len(task_text.encode("utf-8")) // 4)
        output_tokens = 512 if selected_owner != "local_llm" else 128
        if management_profile == "strict_gateway":
            output_tokens = 768
        if management_profile == "coding_supervisor":
            output_tokens = 1024

        return {
            "provider": "mistral",
            "role": "default_external_provider",
            "task_type": task_type,
            "complexity": complexity,
            "priority": priority,
            "high_risk": high_risk,
            "preferred_model": preferred_model,
            "selected_owner": selected_owner,
            "management_profile": management_profile,
            "authority_tier": authority_tier,
            "can_manage_local_llms": selected_owner == "mistral_gateway",
            "acts_as_gateway": selected_owner == "mistral_gateway",
            "delegation_plan": delegation_plan,
            "gateway_conditions": {
                "provider_ready": provider_ready,
                "local_ready": local_ready,
                "budget_available": current_budget > 0.0,
                "supervision_required": management_profile == "strict_gateway",
            },
            "responsibilities": {
                "mistral_executes": ["code", "fix", "refactor", "review", "analysis", "structured summarization"],
                "mistral_manages": ["local_llm_decomposition", "task_normalization", "subtask_assignment"] if selected_owner == "mistral_gateway" else [],
                "local_llm_executes": [item["objective"] for item in delegation_plan],
            },
            "cost_estimate": self.estimate_cost(preferred_model, input_tokens=input_tokens, output_tokens=output_tokens),
        }
