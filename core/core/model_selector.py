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

logger = logging.getLogger(__name__)

# Model Definitions
MODEL_QWEN_CODER = "qwen2.5:32b-instruct-q4_k_m"
MODEL_DEEPSEEK_R1 = "deepseek-r1:14b"
MODEL_LOCAL_SMALL = "qwen-2.5-7b-instruct"

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
        self.openai_router = OpenAIRuntimeRouter()
        self.qwen_router = QwenRuntimeRouter()
        self.model_lifecycle = ModelLifecycleManager()
        self._api: Any | None = None
        self.mimo_bridge = MimoBridge()
        self.mimo_models: list[MimoModel] = []
        self.experience_policy = ExperiencePolicyLearner()

    def sync_with_mimo(self) -> None:
        self.mimo_models = self.mimo_bridge.get_models()

    def set_api(self, api: Any) -> None:
        self._api = api

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
    def _local_code_choice(complexity: Complexity) -> ModelChoice:
        return ModelChoice(
            MODEL_QWEN_CODER,
            "local",
            complexity,
            params=ModelParams(temperature=0.2, context_depth=2),
            requires_secondary_review=False,
            reason="standard_code_qwen_local",
        )

    @staticmethod
    def _local_planning_choice(task: Task, complexity: Complexity) -> ModelChoice:
        if task.type == TaskType.REVIEW:
            return ModelChoice(
                MODEL_DEEPSEEK_R1,
                "local",
                complexity,
                params=ModelParams(temperature=0.2, context_depth=4),
                requires_secondary_review=True,
                reason="review_deepseek_local",
            )

        return ModelChoice(
            MODEL_DEEPSEEK_R1,
            "local",
            complexity,
            params=ModelParams(temperature=0.6, context_depth=3),
            requires_secondary_review=False,
            reason="planning_docs_deepseek_local",
        )

    def _local_policy_choice(self, task: Task, complexity: Complexity, advisory_context: dict[str, Any] | None) -> ModelChoice | None:
        local_choice = self._local_llm_choice(task, complexity, advisory_context)
        if local_choice is not None:
            return local_choice

        if task.type in {TaskType.CODE, TaskType.TEST, TaskType.FIX}:
            return self._local_code_choice(complexity)

        if task.type in {TaskType.PLAN, TaskType.DOCS, TaskType.RESEARCH, TaskType.REVIEW}:
            return self._local_planning_choice(task, complexity)

        return ModelChoice(
            MODEL_QWEN_CODER,
            "local",
            complexity,
            params=ModelParams(temperature=0.5, context_depth=1),
            requires_secondary_review=False,
            reason="policy_default_utility_qwen",
        )

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
            return ModelChoice(model_name, "local", complexity, params=ModelParams(temperature=temperature, context_depth=min(6, context_depth)), requires_secondary_review=False, reason=f"{reason_prefix}_{task_family}")

        if task.type == TaskType.PLAN and complexity in {Complexity.LOW, Complexity.MEDIUM} and (should_delegate or is_primary_owner):
            model_name = preferred_model or MODEL_LOCAL_SMALL
            temperature = 0.65 if budget_pressure == "high" else 0.8
            context_depth = max(2, context_depth_hint or 0)
            if profile_weights:
                context_depth += 1 if float(profile_weights.get("quality", 1.0)) > 1.25 else 0
            reason_prefix = "local_llm_primary_owner" if is_primary_owner else "local_llm_plan_hand_off"
            return ModelChoice(model_name, "local", complexity, params=ModelParams(temperature=temperature, context_depth=min(6, context_depth)), requires_secondary_review=True, reason=f"{reason_prefix}_{task_family}")

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
        return ModelChoice(
            recommended_model,
            str(recommendation.get("provider") or choice.provider),
            choice.complexity,
            params=choice.params,
            requires_secondary_review=choice.requires_secondary_review,
            detected_keywords=choice.detected_keywords,
            matched_high_risk_rules=choice.matched_high_risk_rules,
            matched_low_risk_exemptions=choice.matched_low_risk_exemptions,
            reason=f"experience_policy:{task.type.value}:{recommended_model}:score={learned_score:.2f}:samples={learned_samples}",
        )

    def _openai_choice(self, task: Task, complexity: Complexity, secondary_review: bool, reason: str, fallback_model: str) -> ModelChoice:
        if not OpenAIRuntimeRouter.enabled():
            return ModelChoice(fallback_model, "openai", complexity, params=ModelParams(temperature=0.7), requires_secondary_review=secondary_review, reason=reason)
        if not os.getenv("OPENAI_API_KEY", "").strip():
            if os.getenv("MISTRAL_API_KEY", "").strip():
                return ModelChoice("mistral-large-latest", "mistral", complexity, params=ModelParams(temperature=0.7), requires_secondary_review=secondary_review, reason=f"openai_auto_no_key_mistral_fallback:{reason}")
            return ModelChoice("antigravity-cli", "antigravity", complexity, params=ModelParams(temperature=0.7), requires_secondary_review=secondary_review, reason=f"openai_auto_no_key_antigravity_fallback:{reason}")
        plan = self.openai_router.build_plan(task, task.input.description)
        return ModelChoice(plan.models[0], "openai", complexity, params=ModelParams(temperature=0.7), requires_secondary_review=secondary_review, reason=f"openai_auto_{plan.reason}:{reason}")

    def _attach_selection_trace(self, choice: ModelChoice, task: Task, advisory_context: dict[str, Any] | None = None) -> ModelChoice:
        local = self._local_llm_advisory(advisory_context) or {}
        choice.selection_trace = {
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

        if self._should_escalate_to_cloud(task, complexity, risk):
            choice = self._apply_experience_policy(task, self._openai_choice(task, complexity, True, "high_risk_or_complexity_escalation", "gpt-4o"))
            return self._attach_selection_trace(choice, task, advisory_context)

        local_choice = self._local_policy_choice(task, complexity, advisory_context)
        if local_choice:
            choice = self._apply_experience_policy(task, local_choice)
            return self._attach_selection_trace(choice, task, advisory_context)

        choice = self._apply_experience_policy(
            task,
            ModelChoice(
                MODEL_QWEN_CODER,
                "local",
                complexity,
                params=ModelParams(temperature=0.5, context_depth=1),
                requires_secondary_review=False,
                reason="policy_default_utility_qwen",
            ),
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
