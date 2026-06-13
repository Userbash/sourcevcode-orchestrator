from __future__ import annotations

import os
from enum import Enum
from dataclasses import dataclass, field
from typing import Any

from .control_profiles import ControlProfileRegistry

class ExecutionMode(Enum):
    MANUAL = "manual"
    ASSISTED = "assisted"
    AUTO_SAFE = "auto_safe"
    FULL_AUTO = "full_auto"
    CI = "ci"

class RiskLevel(Enum):
    SAFE = "safe"
    MODERATE = "moderate"
    HIGH = "high"
    DESTRUCTIVE = "destructive"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    PRODUCTION_CRITICAL = "production_critical"

class RiskClassifier:
    @staticmethod
    def classify(task: Any) -> RiskLevel:
        markers = set(_get_value(task, "markers", []) or [])
        action = str(_get_value(task, "action", "") or _get_value(task, "type", "")).lower()

        if "production_critical" in markers or action in {"production_deploy"}:
            return RiskLevel.PRODUCTION_CRITICAL
        if "destructive" in markers or action in {"database_delete", "force_push"}:
            return RiskLevel.DESTRUCTIVE
        if "external" in markers or action in {"external_api_calls"}:
            return RiskLevel.EXTERNAL_SIDE_EFFECT
        if action in {"security", "secret_rotation"}:
            return RiskLevel.HIGH
        if action in {"code_generation", "test", "refactor"}:
            return RiskLevel.SAFE
        return RiskLevel.MODERATE

class PolicyEngine:
    @staticmethod
    def evaluate(task: Any) -> bool:
        risk = RiskClassifier.classify(task)
        if risk in {RiskLevel.DESTRUCTIVE, RiskLevel.PRODUCTION_CRITICAL}:
            return False
        return True

SAFE_STANDARD_TASK_TYPES = {
    "code_generation",
    "code",
    "tests",
    "test",
    "review",
    "docs",
    "documentation",
    "refactor",
    "local_scripts",
    "healthcheck",
    "metrics",
    "task_routing",
}
...

DESTRUCTIVE_MARKERS = {
    "production_deploy",
    "database_delete",
    "delete_production_data",
    "secret_change",
    "rotate_keys",
    "payment_action",
    "modify_billing",
    "external_email",
    "public_api_mutation",
    "force_push",
}


@dataclass(slots=True)
class ConfirmationPolicy:
    ask_for_low_risk_tasks: bool = False
    ask_for_medium_risk_tasks: bool = False
    ask_for_high_risk_tasks: bool = False
    ask_for_destructive_actions: bool = False
    ask_for_external_api_calls: bool = False


@dataclass(slots=True)
class SafetyGuards:
    allow_auto_code_changes: bool = True
    allow_auto_tests: bool = True
    allow_auto_docs: bool = True
    allow_auto_refactor: bool = True
    require_confirmation_for: list[str] = field(default_factory=lambda: sorted(DESTRUCTIVE_MARKERS))


@dataclass(slots=True)
class OrchestrationConfig:
    enabled_by_default: bool = True
    ask_confirmation: bool = False
    execution_mode: str = ExecutionMode.FULL_AUTO.value
    default_mode: str = "core"
    auto_route_tasks: bool = True
    auto_start_agents: bool = True
    auto_retry: bool = True
    auto_review: bool = True
    auto_test: bool = True
    auto_approve_safe_tasks: bool = True
    require_confirmation_for_destructive: bool = False
    blocked_risk_levels: list[str] = field(default_factory=list)
    default_engine: str = "core"
    non_interactive: bool = True
    training_consolidation_interval_sec: int = 300
    trained_memory_quality_threshold: float = 0.75
    trained_memory_quality_thresholds_by_task: dict[str, float] = field(default_factory=lambda: {
        "plan": 0.80,
        "review": 0.85,
        "test": 0.82,
        "code": 0.78,
        "docs": 0.70,
        "research": 0.72,
    })
    trained_memory_cache_ttl_sec: int = 600
    trained_memory_brief_ttl_sec: int = 600
    trained_memory_degrade_ttl_sec: int = 900
    high_risk_trained_memory_enabled: bool = False
    kpi_thresholds_by_task: dict[str, float] = field(default_factory=lambda: {
        "plan": 0.72,
        "review": 0.76,
        "test": 0.74,
    })
    kpi_routing_floor_by_task: dict[str, float] = field(default_factory=lambda: {
        "plan": 0.68,
        "review": 0.70,
        "test": 0.69,
    })
    kpi_rejection_summary_path: str = ""
    kpi_dashboard_interval_sec: int = 3600
    kpi_dashboard_output_path: str = "memory_store/kpi_dashboard_24h.json"
    confirmation_policy: ConfirmationPolicy = field(default_factory=ConfirmationPolicy)
    safety_guards: SafetyGuards = field(default_factory=SafetyGuards)

    @classmethod
    def from_env(cls) -> OrchestrationConfig:
        profiles = ControlProfileRegistry()
        enabled = _env_bool("AI_BRIDGE_ENABLED", True) and _env_bool("AI_BRIDGE_DEFAULT", True)
        auto_approve = _env_bool("AI_BRIDGE_AUTO_APPROVE", True)
        non_interactive = _env_bool("AI_BRIDGE_NON_INTERACTIVE", True)
        execution_mode = os.getenv("AI_BRIDGE_EXECUTION_MODE", ExecutionMode.FULL_AUTO.value).strip().lower() or ExecutionMode.FULL_AUTO.value
        if execution_mode not in {mode.value for mode in ExecutionMode}:
            execution_mode = ExecutionMode.FULL_AUTO.value
        policy_env = os.getenv("AI_BRIDGE_CONFIRMATION_POLICY", execution_mode).strip().lower() or execution_mode
        if policy_env == "safe-only":
            policy_env = ExecutionMode.AUTO_SAFE.value
        execution_profile = profiles.get(execution_mode)
        selected_profile = profiles.get(policy_env if policy_env in {mode.value for mode in ExecutionMode} else execution_profile.slug)
        full_auto = selected_profile.slug == ExecutionMode.FULL_AUTO.value

        return cls(
            enabled_by_default=enabled,
            ask_confirmation=selected_profile.ask_confirmation,
            execution_mode=execution_profile.slug,
            auto_approve_safe_tasks=selected_profile.auto_approve_safe_tasks if full_auto else auto_approve and selected_profile.auto_approve_safe_tasks,
            non_interactive=non_interactive or selected_profile.non_interactive_default,
            training_consolidation_interval_sec=_env_int("AI_BRIDGE_TRAINING_CONSOLIDATION_INTERVAL_SEC", 300),
            trained_memory_quality_threshold=_env_float("AI_BRIDGE_TRAINED_MEMORY_QUALITY_THRESHOLD", 0.75),
            trained_memory_quality_thresholds_by_task={
                "plan": _env_float("AI_BRIDGE_TRAINED_MEMORY_QUALITY_THRESHOLD_PLAN", 0.80),
                "review": _env_float("AI_BRIDGE_TRAINED_MEMORY_QUALITY_THRESHOLD_REVIEW", 0.85),
                "test": _env_float("AI_BRIDGE_TRAINED_MEMORY_QUALITY_THRESHOLD_TEST", 0.82),
                "code": _env_float("AI_BRIDGE_TRAINED_MEMORY_QUALITY_THRESHOLD_CODE", 0.78),
                "docs": _env_float("AI_BRIDGE_TRAINED_MEMORY_QUALITY_THRESHOLD_DOCS", 0.70),
                "research": _env_float("AI_BRIDGE_TRAINED_MEMORY_QUALITY_THRESHOLD_RESEARCH", 0.72),
            },
            trained_memory_cache_ttl_sec=_env_int("AI_BRIDGE_TRAINED_MEMORY_CACHE_TTL_SEC", 600),
            trained_memory_brief_ttl_sec=_env_int("AI_BRIDGE_TRAINED_MEMORY_BRIEF_TTL_SEC", 600),
            trained_memory_degrade_ttl_sec=_env_int("AI_BRIDGE_TRAINED_MEMORY_DEGRADE_TTL_SEC", 900),
            high_risk_trained_memory_enabled=False,
            kpi_thresholds_by_task={
                "plan": _env_float("AI_BRIDGE_KPI_THRESHOLD_PLAN", 0.72),
                "review": _env_float("AI_BRIDGE_KPI_THRESHOLD_REVIEW", 0.76),
                "test": _env_float("AI_BRIDGE_KPI_THRESHOLD_TEST", 0.74),
            },
            kpi_routing_floor_by_task={
                "plan": _env_float("AI_BRIDGE_KPI_ROUTING_FLOOR_PLAN", 0.68),
                "review": _env_float("AI_BRIDGE_KPI_ROUTING_FLOOR_REVIEW", 0.70),
                "test": _env_float("AI_BRIDGE_KPI_ROUTING_FLOOR_TEST", 0.69),
            },
            kpi_rejection_summary_path=(os.getenv("AI_BRIDGE_KPI_REJECTION_SUMMARY_PATH") or "").strip(),
            kpi_dashboard_interval_sec=_env_int("AI_BRIDGE_KPI_DASHBOARD_INTERVAL_SEC", 3600),
            kpi_dashboard_output_path=(os.getenv("AI_BRIDGE_KPI_DASHBOARD_OUTPUT_PATH") or "memory_store/kpi_dashboard_24h.json").strip(),
            require_confirmation_for_destructive=selected_profile.require_confirmation_for_destructive,
            blocked_risk_levels=list(selected_profile.blocked_risk_levels),
            confirmation_policy=ConfirmationPolicy(
                ask_for_low_risk_tasks=bool(selected_profile.confirmation_policy.get("ask_for_low_risk_tasks", False)),
                ask_for_medium_risk_tasks=bool(selected_profile.confirmation_policy.get("ask_for_medium_risk_tasks", False)),
                ask_for_high_risk_tasks=bool(selected_profile.confirmation_policy.get("ask_for_high_risk_tasks", not full_auto)),
                ask_for_destructive_actions=bool(selected_profile.confirmation_policy.get("ask_for_destructive_actions", not full_auto)),
                ask_for_external_api_calls=bool(selected_profile.confirmation_policy.get("ask_for_external_api_calls", not full_auto)),
            ),
        )

    def apply_cli_flags(self, *, yes: bool = False, auto: bool = False, use_bridge: bool = False, non_interactive: bool = False, high_risk_trained_memory: bool = False) -> None:
        if yes or auto:
            self.ask_confirmation = False
            self.execution_mode = ExecutionMode.FULL_AUTO.value
            self.auto_approve_safe_tasks = True
            self.require_confirmation_for_destructive = False
            self.blocked_risk_levels = []
            self.confirmation_policy = ConfirmationPolicy()
        if use_bridge:
            self.enabled_by_default = True
            self.default_mode = "core"
            self.default_engine = "core"
        if non_interactive:
            self.non_interactive = True
        if high_risk_trained_memory:
            self.high_risk_trained_memory_enabled = True

    def should_ask_confirmation(self, task: Any) -> bool:
        if self.execution_mode == ExecutionMode.FULL_AUTO.value:
            return False
        if self.execution_mode == ExecutionMode.MANUAL.value:
            return True
        if not self.enabled_by_default:
            return True
        if str(RiskClassifier.classify(task).value).lower() in set(self.blocked_risk_levels):
            return True
        if _get_bool(task, "manual_only"):
            return True
        if _get_bool(task, "is_destructive") and self.confirmation_policy.ask_for_destructive_actions:
            return True
        if _get_bool(task, "requires_external_side_effect") and self.confirmation_policy.ask_for_external_api_calls:
            return True

        action = str(_get_value(task, "action", "") or _get_value(task, "type", "")).lower()
        markers = set(_get_value(task, "markers", []) or [])
        marker_hit = action in DESTRUCTIVE_MARKERS or bool(markers.intersection(DESTRUCTIVE_MARKERS))
        if marker_hit and self.require_confirmation_for_destructive:
            return True

        risk_level = str(_get_value(task, "risk_level", "low") or "low").lower()
        if risk_level in {"critical", "unsafe"}:
            return True
        if risk_level == "high":
            return self.confirmation_policy.ask_for_high_risk_tasks
        if risk_level == "medium":
            return self.confirmation_policy.ask_for_medium_risk_tasks
        if risk_level == "low":
            return self.confirmation_policy.ask_for_low_risk_tasks

        if action in SAFE_STANDARD_TASK_TYPES and self.auto_approve_safe_tasks:
            return False
        return self.ask_confirmation

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled_by_default": self.enabled_by_default,
            "ask_confirmation": self.ask_confirmation,
            "execution_mode": self.execution_mode,
            "default_mode": self.default_mode,
            "auto_route_tasks": self.auto_route_tasks,
            "auto_start_agents": self.auto_start_agents,
            "auto_retry": self.auto_retry,
            "auto_review": self.auto_review,
            "auto_test": self.auto_test,
            "auto_approve_safe_tasks": self.auto_approve_safe_tasks,
            "require_confirmation_for_destructive": self.require_confirmation_for_destructive,
            "blocked_risk_levels": self.blocked_risk_levels,
            "default_engine": self.default_engine,
            "non_interactive": self.non_interactive,
            "training_consolidation_interval_sec": self.training_consolidation_interval_sec,
            "trained_memory_quality_threshold": self.trained_memory_quality_threshold,
            "trained_memory_quality_thresholds_by_task": self.trained_memory_quality_thresholds_by_task,
            "trained_memory_cache_ttl_sec": self.trained_memory_cache_ttl_sec,
            "trained_memory_brief_ttl_sec": self.trained_memory_brief_ttl_sec,
            "trained_memory_degrade_ttl_sec": self.trained_memory_degrade_ttl_sec,
            "high_risk_trained_memory_enabled": self.high_risk_trained_memory_enabled,
            "kpi_thresholds_by_task": self.kpi_thresholds_by_task,
            "kpi_routing_floor_by_task": self.kpi_routing_floor_by_task,
            "kpi_rejection_summary_path": self.kpi_rejection_summary_path,
            "kpi_dashboard_interval_sec": self.kpi_dashboard_interval_sec,
            "kpi_dashboard_output_path": self.kpi_dashboard_output_path,
        }


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_bool(task: Any, name: str) -> bool:
    return bool(_get_value(task, name, False))


def _get_value(task: Any, name: str, default: Any = None) -> Any:
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default)
