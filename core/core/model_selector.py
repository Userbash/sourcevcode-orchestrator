from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from .models import Complexity, Priority, Task, TaskType
from .openai_runtime_router import OpenAIRuntimeRouter
from .qwen_runtime_router import QwenRuntimeRouter
from .model_lifecycle import ModelLifecycleManager

logger = logging.getLogger(__name__)



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
    requires_secondary_review: bool = False
    detected_keywords: list[str] | None = None
    matched_high_risk_rules: list[str] | None = None
    matched_low_risk_exemptions: list[str] | None = None
    reason: str = "policy_default"


class ModelSelector:
    def __init__(self) -> None:
        self.policy_mode = os.getenv("AI_BRIDGE_POLICY_MODE", "legacy").strip().lower()
        self.openai_router = OpenAIRuntimeRouter()
        self.qwen_router = QwenRuntimeRouter()
        self.model_lifecycle = ModelLifecycleManager()
        self._api: Any | None = None



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

        # Try AI-powered advisor first
        advisor_eval = self._evaluate_with_advisor(task)
        if advisor_eval and advisor_eval.high_risk:
            return Complexity.CRITICAL

        text: str = task.input.description.lower()
        risk: RiskEvaluation = evaluate_risk_context(text)
        if task.priority == Priority.CRITICAL or risk.high_risk:
            return Complexity.CRITICAL

        # Plan/Review only HIGH if complex keywords are present
        if task.priority == Priority.HIGH or (task.type in {TaskType.PLAN, TaskType.REVIEW} and any(w in text for w in ("architecture", "distributed", "debugging"))):
            return Complexity.HIGH

        if risk.matched_low_risk_exemptions and task.type in {TaskType.DOCS, TaskType.FIX} and len(task.input.files) <= 2 and len(text) < 120:
            return Complexity.LOW
        if task.type in {TaskType.CODE, TaskType.TEST, TaskType.FIX, TaskType.DOCS, TaskType.RESEARCH} or len(task.input.files) > 2:
            return Complexity.MEDIUM
        return Complexity.LOW


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
        if not should_delegate and task.type not in {TaskType.DOCS, TaskType.RESEARCH, TaskType.REVIEW}:
            return None

        if task.type in {TaskType.DOCS, TaskType.RESEARCH, TaskType.REVIEW} and complexity in {Complexity.LOW, Complexity.MEDIUM}:
            return ModelChoice("local-small", "local", complexity, False, reason=f"local_llm_advisory_{task_family}")

        if task.type == TaskType.PLAN and should_delegate and complexity in {Complexity.LOW, Complexity.MEDIUM}:
            return ModelChoice("local-small", "local", complexity, True, reason=f"local_llm_plan_hand_off_{task_family}")

        return None

    def _openai_choice(self, task: Task, complexity: Complexity, secondary_review: bool, reason: str, fallback_model: str) -> ModelChoice:
        if not OpenAIRuntimeRouter.enabled():
            return ModelChoice(fallback_model, "openai", complexity, secondary_review, reason=reason)
        if not os.getenv("OPENAI_API_KEY", "").strip():
            if os.getenv("MISTRAL_API_KEY", "").strip():
                return ModelChoice("mistral-large-latest", "mistral", complexity, secondary_review, reason=f"openai_auto_no_key_mistral_fallback:{reason}")
            return ModelChoice("antigravity-cli", "antigravity", complexity, secondary_review, reason=f"openai_auto_no_key_antigravity_fallback:{reason}")
        plan = self.openai_router.build_plan(task, task.input.description)
        return ModelChoice(plan.models[0], "openai", complexity, secondary_review, reason=f"openai_auto_{plan.reason}:{reason}")

    def _select_legacy(self, task: Task, complexity: Complexity, advisory_context: dict[str, Any] | None = None) -> ModelChoice:
        local_choice = self._local_llm_choice(task, complexity, advisory_context)
        if local_choice is not None:
            return local_choice
        
        # Risk Check (High risk always to Cloud)
        risk = evaluate_risk_context(task.input.description.lower())
        if complexity == Complexity.CRITICAL or risk.high_risk:
            return self._openai_choice(task, complexity, True, "critical_risk_openai_escalation", "gpt-senior-secure")
        
        # Route based on TaskType to optimized local models
        if task.type in {TaskType.CODE, TaskType.TEST, TaskType.FIX}:
            if complexity == Complexity.HIGH:
                # Complex FIX/CODE -> DeepSeek-R1 (Local, strong reasoning)
                return ModelChoice("deepseek-r1:14b", "local", complexity, True, reason="complex_code_fix_deepseek_local")
            # Standard CODE -> Qwen-Coder (Local)
            return ModelChoice("qwen2.5-coder:14b", "local", complexity, False, reason="standard_code_qwen_local")

        if task.type in {TaskType.PLAN, TaskType.DOCS, TaskType.RESEARCH}:
            # Planning/Docs -> Mistral-Nemo (Local)
            return ModelChoice("mistral-nemo:12b", "local", complexity, False, reason="planning_docs_mistral_local")
        
        if task.type == TaskType.REVIEW:
            return ModelChoice("deepseek-r1:14b", "local", complexity, True, reason="review_deepseek_local")

        return ModelChoice("llama3.2:3b", "local", complexity, False, reason="policy_default_utility")

    def _select_strict(self, task: Task, complexity: Complexity, advisory_context: dict[str, Any] | None = None) -> ModelChoice:
        # strict minimizes OpenAI except explicit critical/high-risk.
        local_choice = self._local_llm_choice(task, complexity, advisory_context)
        if local_choice is not None and task.type in {TaskType.DOCS, TaskType.RESEARCH, TaskType.REVIEW}:
            return local_choice
        if complexity == Complexity.CRITICAL:
            return self._openai_choice(task, complexity, True, "critical_openai_only", "gpt-senior-secure")
        if complexity == Complexity.HIGH:
            if task.type in {TaskType.PLAN, TaskType.REVIEW}:
                return ModelChoice("antigravity-cli", "antigravity", complexity, True, reason="high_plan_review_antigravity")
            return ModelChoice("mistral-large-latest", "mistral", complexity, True, reason="high_noncritical_mistral")
        if task.type in {TaskType.CODE, TaskType.FIX, TaskType.TEST}:
            return ModelChoice("mistral-small-or-medium", "mistral", complexity, False, reason="strict_code_mistral")
        return ModelChoice("antigravity-cli", "antigravity", complexity, False, reason="strict_docs_research_antigravity")

    def select(self, task: Task, advisory_context: dict[str, Any] | None = None) -> ModelChoice:
        complexity = self.classify(task)
        risk = evaluate_risk_context(task.input.description.lower())
        
        # 1. HIGH/CRITICAL Complexity or High Risk -> Cloud
        if complexity in {Complexity.CRITICAL, Complexity.HIGH} or risk.high_risk:
            return self._openai_choice(task, complexity, True, "high_risk_or_complexity_escalation", "gpt-4o")

        # 2. CODE/FIX/TEST -> Qwen-Coder (Local)
        if task.type in {TaskType.CODE, TaskType.TEST, TaskType.FIX}:
            return ModelChoice("qwen2.5-coder:14b", "local", complexity, False, reason="standard_code_qwen_local")
        
        # 3. PLAN/DOCS/RESEARCH -> Mistral-Nemo (Local)
        if task.type in {TaskType.PLAN, TaskType.DOCS, TaskType.RESEARCH}:
             return ModelChoice("mistral-nemo:12b", "local", complexity, False, reason="planning_docs_mistral_local")
        
        # 4. REVIEW -> DeepSeek (Local)
        if task.type == TaskType.REVIEW:
            return ModelChoice("deepseek-r1:14b", "local", complexity, True, reason="review_deepseek_local")

        # Default fallback
        return ModelChoice("llama3.2:3b", "local", complexity, False, reason="policy_default_utility")



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
