from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .models import Complexity, Priority, Task, TaskType, ModelParams
from .openai_runtime_router import OpenAIRuntimeRouter
from .qwen_runtime_router import QwenRuntimeRouter
from .model_lifecycle import ModelLifecycleManager
from .mimo_bridge import MimoBridge, MimoModel
from .experience_policy_learner import ExperiencePolicyLearner
from .provider_credentials import has_usable_credential
from .adaptive_routing_engine import AdaptiveRoutingEngine
from .model_health_registry import ModelHealthRegistry

logger = logging.getLogger(__name__)

# Model Definitions
MODEL_QWEN_CODER = "qwen2.5:32b-instruct-q4_k_m"
MODEL_LOCAL_SMALL = "qwen-2.5-7b-instruct"
MODEL_AI_KERNEL_QWEN36 = os.getenv("AI_KERNEL_MODEL_ALIAS") or "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"

BASE_HIGH_RISK_KEYWORDS = ["security", "auth", "rbac", "payment", "secret", "production", "migration", "destructive"]

PERMISSION_CONTEXT_KEYWORDS = ["auth", "authorization", "role", "rbac", "admin", "security", "token", "database", "migration", "tenant"]
LOW_RISK_PERMISSION_EXEMPTIONS = ["permissions-sync-fix", "permission docs cleanup", "permission ui label", "permission comments", "permission formatting"]


@dataclass(slots=True)
class RiskEvaluation:
    detected_keywords: list[str]
    matched_high_risk_rules: list[str]
    matched_low_risk_exemptions: list[str]
    high_risk: bool


@dataclass(slots=True)
class ModelChoice:
    model_name: str
    provider: str
    complexity: Complexity
    params: ModelParams = field(default_factory=ModelParams)
    requires_secondary_review: bool = False
    detected_keywords: list[str] | None = None
    matched_high_risk_rules: list[str] | None = None
    matched_low_risk_exemptions: list[str] | None = None
    reason: str = "policy_default"
    selection_trace: dict[str, Any] | None = None


class ModelSelector:
    def __init__(self) -> None:
        self.policy_mode = os.getenv("AI_BRIDGE_POLICY_MODE", "legacy").strip().lower()
        self._configured_credentials = {
            "openai": self._explicit_credential_present("OPENAI_API_KEY", "CODEX_SALE_API_KEY"),
            "mistral": self._explicit_credential_present("MISTRAL_API_KEY"),
            "antigravity": self._explicit_credential_present("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "mimo": self._explicit_credential_present("MIMO_API_KEY", "AI_BRIDGE_MIMO_API_KEY"),
        }
        self.openai_router = OpenAIRuntimeRouter()
        self.qwen_router = QwenRuntimeRouter()
        self.model_lifecycle = ModelLifecycleManager()
        self._api: Any | None = None
        self.mimo_bridge = MimoBridge()
        self.mimo_models: list[MimoModel] = []
        self.experience_policy = ExperiencePolicyLearner()
        self.model_health = ModelHealthRegistry()
        self.adaptive_router = AdaptiveRoutingEngine(
            openai_router=self.openai_router,
            experience_policy=self.experience_policy,
        )

    def sync_with_mimo(self) -> None:
        self.mimo_models = self.mimo_bridge.get_models()

    def set_api(self, api: Any) -> None:
        self._api = api
        self.adaptive_router.bus = getattr(api, "message_bus", None)

    @staticmethod
    def _adaptive_context_depth(complexity: Complexity) -> int:
        if complexity == Complexity.CRITICAL:
            return 5
        if complexity == Complexity.HIGH:
            return 4
        if complexity == Complexity.MEDIUM:
            return 3
        return 2

    def _adaptive_choice(
        self,
        task: Task,
        complexity: Complexity,
        advisory_context: dict[str, Any] | None,
        *,
        secondary_review: bool,
    ) -> ModelChoice | None:
        try:
            decision = self.adaptive_router.decide(task, advisory_context)
        except Exception:
            return None
        if decision is None:
            return None
        if self._choice_blocked(decision.primary_provider, decision.primary_model):
            return None
        if not self._provider_is_usable(decision.primary_provider):
            return None
        return ModelChoice(
            decision.primary_model,
            decision.primary_provider,
            complexity,
            params=ModelParams(temperature=0.35, context_depth=self._adaptive_context_depth(complexity)),
            requires_secondary_review=secondary_review or task.type == TaskType.REVIEW,
            reason=f"adaptive_routing:{decision.role}:{decision.primary_provider}",
            selection_trace=dict(decision.trace),
        )

    @staticmethod
    def _explicit_credential_present(*env_names: str) -> bool:
        return any(str(os.getenv(env_name, "")).strip() for env_name in env_names)

    def _provider_is_usable(self, provider: str) -> bool:
        normalized = str(provider or "").strip().lower()
        if normalized == "openai":
            return bool(self._configured_credentials.get("openai")) and has_usable_credential("OPENAI_API_KEY")
        if normalized == "mistral":
            return bool(self._configured_credentials.get("mistral")) and has_usable_credential("MISTRAL_API_KEY")
        if normalized == "antigravity":
            return bool(self._configured_credentials.get("antigravity")) and has_usable_credential("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        if normalized == "mimo":
            return bool(self._configured_credentials.get("mimo")) and has_usable_credential("MIMO_API_KEY", "AI_BRIDGE_MIMO_API_KEY")
        if normalized in {"local", "ai_kernel"}:
            return True
        return True

    def _evaluate_with_advisor(self, task: Task) -> RiskEvaluation | None:
        if not self._api:
            return None
        
        advisor = self._api.get_module("risk_advisor")
        if not advisor:
            return None
            
        assessment = advisor.evaluate_task(task)
        if not assessment:
            return None
            
        return RiskEvaluation(
            detected_keywords=assessment.impact_areas,
            matched_high_risk_rules=[assessment.justification],
            matched_low_risk_exemptions=[],
            high_risk=assessment.risk_score > 0.6
        )

    def classify(self, task: Task) -> Complexity:
        if task.complexity:
            return task.complexity

        advisor_eval = self._evaluate_with_advisor(task)
        if advisor_eval and advisor_eval.high_risk:
            return Complexity.CRITICAL

        text: str = self._task_text(task)
        risk: RiskEvaluation = evaluate_risk_context(text)
        if task.priority == Priority.CRITICAL or risk.high_risk:
            return Complexity.CRITICAL

        if task.priority == Priority.HIGH or (task.type in {TaskType.PLAN, TaskType.REVIEW} and any(w in text for w in ("architecture", "distributed", "debugging"))):
            return Complexity.HIGH

        if risk.matched_low_risk_exemptions and task.type in {TaskType.DOCS, TaskType.FIX} and len(task.input.files) <= 2 and len(text) < 120:
            return Complexity.LOW
        if task.type in {TaskType.CODE, TaskType.TEST, TaskType.FIX, TaskType.DOCS, TaskType.RESEARCH} or len(task.input.files) > 2:
            return Complexity.MEDIUM
        return Complexity.LOW

    @staticmethod
    def _task_text(task: Task) -> str:
        return task.input.description.lower().strip()

    def _should_escalate_to_cloud(self, task: Task, complexity: Complexity, risk: RiskEvaluation) -> bool:
        return complexity in {Complexity.CRITICAL, Complexity.HIGH} or task.priority == Priority.CRITICAL or risk.high_risk

    @staticmethod
    def _ai_kernel_enabled() -> bool:
        return os.getenv("AI_KERNEL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


    def _selected_model_health(self, provider: str, model_name: str) -> dict[str, Any] | None:
        payload = self.model_health.load()
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        provider_keys = {str(provider or "").strip().lower()}
        if "local" in provider_keys:
            provider_keys.add("local_llm")
        if "local_llm" in provider_keys:
            provider_keys.add("local")
        normalized_model = str(model_name or "").strip()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("model_name") or "").strip() != normalized_model:
                continue
            if str(row.get("provider") or "").strip().lower() in provider_keys:
                return row
        return None

    def _query_local_model_manager_state(self, key: str, default: Any) -> Any:
        if self._api is None or not hasattr(self._api, "query_module_state"):
            return default
        try:
            value = self._api.query_module_state("local_model_manager", key)
        except Exception:
            return default
        return default if value is None else value

    def _blocked_model_keys(self) -> set[tuple[str, str]]:
        blocked = self._query_local_model_manager_state("blocked_models", [])
        keys: set[tuple[str, str]] = set()
        if not isinstance(blocked, list):
            return keys
        for item in blocked:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").strip().lower()
            model_name = str(item.get("model_name") or "").strip()
            if provider and model_name:
                keys.add((provider, model_name))
        return keys

    def _choice_blocked(self, provider: str, model_name: str) -> bool:
        return (str(provider).strip().lower(), str(model_name).strip()) in self._blocked_model_keys()

    def _first_unblocked_choice(self, candidates: list[ModelChoice]) -> ModelChoice | None:
        for choice in candidates:
            if not self._choice_blocked(choice.provider, choice.model_name):
                return choice
        return None

    def _local_code_choice(self, complexity: Complexity) -> ModelChoice | None:
        candidates: list[ModelChoice] = []
        if ModelSelector._ai_kernel_enabled():
            candidates.append(ModelChoice(
                MODEL_AI_KERNEL_QWEN36,
                "ai_kernel",
                complexity,
                params=ModelParams(temperature=0.15, context_depth=4),
                requires_secondary_review=False,
                reason="standard_code_ai_kernel_qwen36",
            ))
        candidates.append(ModelChoice(
            MODEL_QWEN_CODER,
            "local",
            complexity,
            params=ModelParams(temperature=0.2, context_depth=2),
            requires_secondary_review=False,
            reason="standard_code_qwen_local",
        ))
        return self._first_unblocked_choice(candidates)

    def _local_planning_choice(self, task: Task, complexity: Complexity) -> ModelChoice | None:
        candidates: list[ModelChoice] = []
        if task.type == TaskType.REVIEW:
            candidates.append(ModelChoice(
                MODEL_LOCAL_SMALL,
                "local",
                complexity,
                params=ModelParams(temperature=0.2, context_depth=4),
                requires_secondary_review=True,
                reason="review_qwen_local",
            ))
        else:
            candidates.append(ModelChoice(
                MODEL_LOCAL_SMALL,
                "local",
                complexity,
                params=ModelParams(temperature=0.6, context_depth=3),
                requires_secondary_review=False,
                reason="planning_docs_qwen_local",
            ))
        if ModelSelector._ai_kernel_enabled() and task.type in {TaskType.PLAN, TaskType.RESEARCH, TaskType.REVIEW}:
            candidates.append(ModelChoice(
                MODEL_AI_KERNEL_QWEN36,
                "ai_kernel",
                complexity,
                params=ModelParams(temperature=0.35, context_depth=3),
                requires_secondary_review=task.type == TaskType.REVIEW,
                reason="planning_fallback_ai_kernel_qwen36",
            ))
        candidates.append(ModelChoice(
            MODEL_QWEN_CODER,
            "local",
            complexity,
            params=ModelParams(temperature=0.4, context_depth=2),
            requires_secondary_review=False,
            reason="planning_fallback_qwen_local",
        ))
        return self._first_unblocked_choice(candidates)

    def _local_policy_choice(self, task: Task, complexity: Complexity, advisory_context: dict[str, Any] | None) -> ModelChoice | None:
        local_choice = self._local_llm_choice(task, complexity, advisory_context)
        if local_choice is not None:
            return local_choice

        if task.type in {TaskType.CODE, TaskType.TEST, TaskType.FIX}:
            return self._local_code_choice(complexity)

        if task.type in {TaskType.PLAN, TaskType.DOCS, TaskType.RESEARCH, TaskType.REVIEW}:
            return self._local_planning_choice(task, complexity)

        utility_choice = ModelChoice(
            MODEL_QWEN_CODER,
            "local",
            complexity,
            params=ModelParams(temperature=0.5, context_depth=1),
            requires_secondary_review=False,
            reason="policy_default_utility_qwen",
        )
        return None if self._choice_blocked(utility_choice.provider, utility_choice.model_name) else utility_choice

    @staticmethod
    def _local_llm_advisory(advisory_context: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(advisory_context, dict):
            return None
        local = advisory_context.get("local_llm")
        return local if isinstance(local, dict) else None

    def _local_llm_choice(self, task: Task, complexity: Complexity, advisory_context: dict[str, Any] | None) -> ModelChoice | None:
        local = self._local_llm_advisory(advisory_context)
        if not local or not local.get("ready"):
            return None
        if task.priority == Priority.CRITICAL or complexity == Complexity.CRITICAL:
            return None

        task_family = str(local.get("task_family") or "general")
        should_delegate = bool(local.get("should_delegate"))
        recommended_owner = str(local.get("recommended_owner") or "").strip().lower()
        preferred_model = str(local.get("preferred_model") or local.get("recommended_model") or local.get("model_hint") or "").strip()
        budget_pressure = str(local.get("budget_pressure") or "normal")
        context_depth_hint = int(local.get("context_depth") or 0)
        profile_weights = local.get("profile_weights") if isinstance(local.get("profile_weights"), dict) else {}
        local_primary_types = {TaskType.PLAN, TaskType.DOCS, TaskType.RESEARCH, TaskType.REVIEW}
        is_primary_owner = (
            recommended_owner == "local_llm"
            and task.type in local_primary_types
            and complexity in {Complexity.LOW, Complexity.MEDIUM}
        )
        if not should_delegate and not is_primary_owner:
            return None

        if task.type in {TaskType.DOCS, TaskType.RESEARCH, TaskType.REVIEW} and complexity in {Complexity.LOW, Complexity.MEDIUM}:
            model_name = preferred_model or MODEL_LOCAL_SMALL
            temperature = 0.35 if budget_pressure in {"high", "medium"} else 0.5
            context_depth = max(1, context_depth_hint or 0)
            if profile_weights:
                context_depth += 1 if float(profile_weights.get("quality", 1.0)) > 1.2 else 0
            reason_prefix = "local_llm_primary_owner" if is_primary_owner else "local_llm_advisory"
            choice = ModelChoice(model_name, "local", complexity, params=ModelParams(temperature=temperature, context_depth=min(6, context_depth)), requires_secondary_review=False, reason=f"{reason_prefix}_{task_family}")
            return None if self._choice_blocked(choice.provider, choice.model_name) else choice

        if task.type == TaskType.PLAN and complexity in {Complexity.LOW, Complexity.MEDIUM} and (should_delegate or is_primary_owner):
            model_name = preferred_model or MODEL_LOCAL_SMALL
            temperature = 0.65 if budget_pressure == "high" else 0.8
            context_depth = max(2, context_depth_hint or 0)
            if profile_weights:
                context_depth += 1 if float(profile_weights.get("quality", 1.0)) > 1.25 else 0
            reason_prefix = "local_llm_primary_owner" if is_primary_owner else "local_llm_plan_hand_off"
            choice = ModelChoice(model_name, "local", complexity, params=ModelParams(temperature=temperature, context_depth=min(6, context_depth)), requires_secondary_review=True, reason=f"{reason_prefix}_{task_family}")
            return None if self._choice_blocked(choice.provider, choice.model_name) else choice

        return None

    def _apply_experience_policy(self, task: Task, choice: ModelChoice) -> ModelChoice:
        learner = getattr(self._api, "experience_policy_learner", None) if self._api else None
        if learner is None or task.priority == Priority.CRITICAL or choice.complexity == Complexity.CRITICAL:
            return choice

        allowed_providers = {choice.provider}
        recommendation = learner.recommend_model(task_type=task.type.value, allowed_providers=allowed_providers)
        if recommendation is None:
            return choice
        recommended_model = str(recommendation.get("model_name") or "").strip()
        if not recommended_model or recommended_model == choice.model_name:
            return choice

        learned_score = float(recommendation.get("score") or 0.0)
        learned_samples = int(recommendation.get("samples") or 0)
        recommended_provider = str(recommendation.get("provider") or choice.provider)
        if recommended_provider.strip().lower() == "openai":
            sanitized_model = OpenAIRuntimeRouter.sanitize_model(recommended_model)
            if not sanitized_model:
                return choice
            recommended_model = sanitized_model
        return ModelChoice(
            recommended_model,
            recommended_provider,
            choice.complexity,
            params=choice.params,
            requires_secondary_review=choice.requires_secondary_review,
            detected_keywords=choice.detected_keywords,
            matched_high_risk_rules=choice.matched_high_risk_rules,
            matched_low_risk_exemptions=choice.matched_low_risk_exemptions,
            reason=f"experience_policy:{task.type.value}:{recommended_model}:score={learned_score:.2f}:samples={learned_samples}",
        )

    def _openai_choice(self, task: Task, complexity: Complexity, secondary_review: bool, reason: str, fallback_model: str) -> ModelChoice:
        if not self._provider_is_usable("openai"):
            if self._provider_is_usable("mistral"):
                return ModelChoice("mistral-large-latest", "mistral", complexity, params=ModelParams(temperature=0.7), requires_secondary_review=secondary_review, reason=f"openai_auto_no_key_mistral_fallback:{reason}")
            if self._provider_is_usable("antigravity"):
                return ModelChoice("antigravity-pro", "antigravity", complexity, params=ModelParams(temperature=0.7), requires_secondary_review=secondary_review, reason=f"openai_auto_no_key_antigravity_fallback:{reason}")
            fallback_choice = self._local_planning_choice(task, complexity) if task.type in {TaskType.PLAN, TaskType.DOCS, TaskType.RESEARCH, TaskType.REVIEW} else self._local_code_choice(complexity)
            if fallback_choice is not None:
                return fallback_choice
            return ModelChoice(fallback_model, "openai", complexity, params=ModelParams(temperature=0.7), requires_secondary_review=secondary_review, reason=f"openai_unavailable_local_models_blocked:{reason}")
        if not OpenAIRuntimeRouter.enabled():
            return ModelChoice(fallback_model, "openai", complexity, params=ModelParams(temperature=0.7), requires_secondary_review=secondary_review, reason=reason)
        plan = self.openai_router.build_plan(task, task.input.description)
        return ModelChoice(plan.models[0], "openai", complexity, params=ModelParams(temperature=0.7), requires_secondary_review=secondary_review, reason=f"openai_auto_{plan.reason}:{reason}")

    def _attach_selection_trace(self, choice: ModelChoice, task: Task, advisory_context: dict[str, Any] | None = None) -> ModelChoice:
        local = self._local_llm_advisory(advisory_context) or {}
        base_trace = choice.selection_trace if isinstance(choice.selection_trace, dict) else {}
        choice.selection_trace = {
            **base_trace,
            "provider": choice.provider,
            "model_name": choice.model_name,
            "complexity": getattr(choice.complexity, "value", str(choice.complexity)),
            "reason": choice.reason,
            "task_type": getattr(task.type, "value", str(task.type)),
            "budget_pressure": str(local.get("budget_pressure") or "normal"),
            "context_depth": int(getattr(choice.params, "context_depth", 0) or 0),
            "preferred_model": str(local.get("preferred_model") or local.get("recommended_model") or local.get("model_hint") or "").strip(),
            "task_family": str(local.get("task_family") or "general"),
            "requires_secondary_review": bool(choice.requires_secondary_review),
        }
        if self._api and hasattr(self._api, "log"):
            try:
                self._api.log("info", f"[MODEL_SELECTOR] selection_trace={choice.selection_trace}")
            except Exception:
                pass
        return choice

    def select(self, task: Task, advisory_context: dict[str, Any] | None = None) -> ModelChoice:
        complexity = self.classify(task)
        task.complexity = complexity
        risk = evaluate_risk_context(self._task_text(task))
        adaptive_choice = self._adaptive_choice(
            task,
            complexity,
            advisory_context,
            secondary_review=self._should_escalate_to_cloud(task, complexity, risk),
        )

        if self._should_escalate_to_cloud(task, complexity, risk):
            if adaptive_choice and adaptive_choice.provider != "local":
                return self._attach_selection_trace(adaptive_choice, task, advisory_context)
            choice = self._apply_experience_policy(task, self._openai_choice(task, complexity, True, "high_risk_or_complexity_escalation", "gpt-5.5"))
            return self._attach_selection_trace(choice, task, advisory_context)

        if adaptive_choice is not None:
            return self._attach_selection_trace(adaptive_choice, task, advisory_context)

        local_choice = self._local_policy_choice(task, complexity, advisory_context)
        if local_choice:
            choice = self._apply_experience_policy(task, local_choice)
            return self._attach_selection_trace(choice, task, advisory_context)

        choice = self._apply_experience_policy(
            task,
            self._openai_choice(task, complexity, False, "local_models_unavailable", "gpt-5.4-mini"),
        )
        return self._attach_selection_trace(choice, task, advisory_context)

def evaluate_risk_context(text: str) -> RiskEvaluation:
    normalized = text.lower()
    detected_keywords: list[str] = []
    matched_high_risk_rules: list[str] = []
    matched_low_risk_exemptions: list[str] = []

    for k in BASE_HIGH_RISK_KEYWORDS:
        if k in normalized:
            detected_keywords.append(k)
            matched_high_risk_rules.append(f"base:{k}")

    has_permission = "permission" in normalized or "permissions" in normalized
    if has_permission:
        detected_keywords.append("permission")
        for k in PERMISSION_CONTEXT_KEYWORDS:
            if k in normalized:
                matched_high_risk_rules.append(f"permission+{k}")
        for p in LOW_RISK_PERMISSION_EXEMPTIONS:
            if p in normalized:
                matched_low_risk_exemptions.append(p)

    if matched_low_risk_exemptions and has_permission:
        matched_high_risk_rules = [r for r in matched_high_risk_rules if not r.startswith("permission+")]

    return RiskEvaluation(sorted(set(detected_keywords)), sorted(set(matched_high_risk_rules)), sorted(set(matched_low_risk_exemptions)), bool(matched_high_risk_rules))
