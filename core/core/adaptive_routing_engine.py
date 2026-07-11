from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .experience_policy_learner import ExperiencePolicyLearner
from .model_routing_policy import ModelRoutingPolicy
from .model_usage_module import ModelUsageModule
from .model_health_registry import ModelHealthRegistry
from .models import Complexity, Task, TaskType
from .openai_runtime_router import OpenAIRuntimeRouter
from .provider_inventory_service import ProviderInventoryService


@dataclass(slots=True)
class RoutingCandidate:
    model_name: str
    provider: str
    role: str
    visible: bool
    workable: bool
    routable: bool
    preferred: bool
    score: float
    learned_score: float = 0.0
    learned_samples: int = 0
    budget_action: str = "ok"
    estimated_cost_usd: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParallelWorkAssignment:
    lane_index: int
    lane_kind: str
    worker_role: str
    target_capability: str
    file_targets: list[str]
    model_name: str
    provider: str
    routing_hints: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AdaptiveRoutingDecision:
    primary_model: str
    primary_provider: str
    role: str
    complexity: Complexity
    candidates: list[RoutingCandidate]
    fallback_models: list[str]
    parallel_assignments: list[ParallelWorkAssignment]
    trace: dict[str, Any] = field(default_factory=dict)


class AdaptiveRoutingEngine:
    def __init__(
        self,
        *,
        inventory_service: ProviderInventoryService | None = None,
        openai_router: OpenAIRuntimeRouter | None = None,
        experience_policy: ExperiencePolicyLearner | None = None,
        model_usage: ModelUsageModule | None = None,
        bus: Any | None = None,
    ) -> None:
        self.inventory_service = inventory_service or ProviderInventoryService()
        self.openai_router = openai_router or OpenAIRuntimeRouter()
        self.experience_policy = experience_policy or ExperiencePolicyLearner()
        self.model_usage = model_usage or ModelUsageModule()
        self.model_health = ModelHealthRegistry()
        self.bus = bus

    @staticmethod
    def _task_complexity(task: Task) -> Complexity:
        return task.complexity or Complexity.MEDIUM

    @staticmethod
    def _task_role(task: Task, complexity: Complexity) -> str:
        if task.type in {TaskType.CODE, TaskType.FIX}:
            return "code_parallel"
        if task.type == TaskType.REVIEW:
            return "review_primary"
        if task.type == TaskType.PLAN:
            return "plan_primary"
        if task.type == TaskType.TEST:
            return "test_primary"
        if task.type == TaskType.DOCS:
            return "docs_primary"
        if task.type == TaskType.RESEARCH:
            return "research_primary"
        return "plan_primary" if complexity in {Complexity.HIGH, Complexity.CRITICAL} else "docs_primary"

    @staticmethod
    def _provider_for_model(model_name: str) -> str:
        lowered = str(model_name or "").strip().lower()
        if not lowered:
            return "unknown"
        if lowered.startswith("gpt-"):
            return "openai"
        if "mistral" in lowered or "codestral" in lowered or "devstral" in lowered:
            return "mistral"
        if any(token in lowered for token in ("gemini", "antigravity")):
            return "antigravity"
        if lowered.startswith("mimo-"):
            return "mimo"
        if any(token in lowered for token in ("qwen", "llama", "gemma", "hauhaucs-")):
            return "local"
        return "local"

    @staticmethod
    def _group_key(file_path: str) -> str:
        normalized = str(file_path or "").strip().replace("\\", "/")
        if not normalized:
            return "misc"
        parts = [part for part in normalized.split("/") if part]
        if len(parts) >= 2:
            return "/".join(parts[:2])
        return parts[0]

    @classmethod
    def _group_files(cls, files: list[str], max_groups: int) -> list[list[str]]:
        if not files:
            return [[]]
        grouped: dict[str, list[str]] = {}
        for file_path in files:
            grouped.setdefault(cls._group_key(file_path), []).append(file_path)
        ordered = sorted(grouped.values(), key=lambda row: (-len(row), row[0]))
        if len(ordered) <= max_groups:
            return [sorted(group) for group in ordered]
        merged: list[list[str]] = [[] for _ in range(max_groups)]
        for index, group in enumerate(ordered):
            merged[index % max_groups].extend(group)
        return [sorted(group) for group in merged if group]

    @staticmethod
    def _lane_kind(index: int, total: int) -> str:
        if total <= 1:
            return "implement"
        if index == 0:
            return "primary"
        if index == total - 1:
            return "integration"
        return "secondary"

    @staticmethod
    def _lane_role(task: Task, lane_kind: str) -> str:
        if task.type == TaskType.REVIEW:
            return "review_primary"
        if task.type == TaskType.TEST:
            return "test_primary"
        if lane_kind == "integration":
            return "review_primary"
        if lane_kind == "primary":
            return "code_parallel"
        return "code_parallel"

    @staticmethod
    def _safe_parallelism(task: Task) -> int:
        hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
        try:
            return max(1, int(hints.get("parallel_branches") or 1))
        except (TypeError, ValueError):
            return 1

    def _visible_provider_models(self) -> dict[str, set[str]]:
        snapshot = self.inventory_service.read_snapshot()
        providers = snapshot.get("providers") if isinstance(snapshot, dict) else {}
        visible: dict[str, set[str]] = {}
        if not isinstance(providers, dict):
            return visible
        for provider, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            rows = entry.get("models")
            models = {str(item).strip() for item in rows if str(item).strip()} if isinstance(rows, list) else set()
            if models:
                visible[str(provider).strip().lower()] = models
        return visible


    def _model_health_payload(self) -> dict[str, Any]:
        snapshot = self.inventory_service.read_snapshot()
        if isinstance(snapshot, dict):
            payload = snapshot.get("model_health")
            if isinstance(payload, dict) and payload.get("models"):
                return payload
        payload = self.model_health.load()
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _display_provider(provider: str) -> str:
        normalized = str(provider or "").strip().lower()
        if normalized == "local_llm":
            return "local"
        return normalized or "unknown"

    def _find_model_health_row(self, provider: str, model_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        current = payload if isinstance(payload, dict) else self._model_health_payload()
        rows = current.get("models") if isinstance(current, dict) else None
        if not isinstance(rows, list):
            return None
        normalized_model = str(model_name or "").strip()
        provider_candidates = {str(provider or "").strip().lower()}
        if "local" in provider_candidates:
            provider_candidates.add("local_llm")
        if "local_llm" in provider_candidates:
            provider_candidates.add("local")
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_model = str(row.get("model_name") or "").strip()
            row_provider = str(row.get("provider") or "").strip().lower()
            if row_model == normalized_model and row_provider in provider_candidates:
                return row
        return None

    @staticmethod
    def _role_models_from_health(payload: dict[str, Any], role: str) -> list[str]:
        roles = payload.get("roles") if isinstance(payload, dict) else None
        if not isinstance(roles, dict):
            return []
        entries = roles.get(role)
        if not isinstance(entries, list):
            return []
        return [str(item).strip() for item in entries if str(item).strip()]

    def _learned_weight(self, task: Task, model_name: str) -> tuple[float, int]:
        task_models = self.experience_policy.weights.get("task_models", {}).get(task.type.value, {})
        if not isinstance(task_models, dict):
            return 0.0, 0
        payload = task_models.get(model_name)
        if not isinstance(payload, dict):
            return 0.0, 0
        return float(payload.get("score") or 0.0), int(payload.get("samples") or 0)

    def _estimate_tokens(self, task: Task) -> int:
        complexity = self._task_complexity(task)
        prompt_tokens = max(8, len(task.input.description or "") // 4)
        if complexity == Complexity.LOW:
            return prompt_tokens + 512
        if complexity == Complexity.MEDIUM:
            return prompt_tokens + 1536
        if complexity == Complexity.HIGH:
            return prompt_tokens + 4096
        return prompt_tokens + 8192

    def _openai_candidates(self, task: Task, complexity: Complexity) -> tuple[list[str], list[str]]:
        try:
            plan = self.openai_router.build_plan(task, task.input.description)
            return list(plan.models), [str(plan.reason)]
        except Exception as exc:
            return [], [f"openai_plan_unavailable:{exc}"]

    def build_candidates(self, task: Task, advisory_context: dict[str, Any] | None = None) -> list[RoutingCandidate]:
        del advisory_context
        complexity = self._task_complexity(task)
        role = self._task_role(task, complexity)
        visible = self._visible_provider_models()
        openai_models, openai_reasons = self._openai_candidates(task, complexity)
        model_health = self._model_health_payload()
        role_models = self._role_models_from_health(model_health, role)
        local_defaults = [
            "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m",
            "qwen2.5:32b-instruct-q4_k_m",
            "qwen-2.5-7b-instruct",
        ]
        preferred_names = set(role_models[:3] or openai_models[:3])
        all_models: list[str] = []
        all_models.extend(role_models)
        all_models.extend(openai_models)
        all_models.extend(local_defaults)
        seen: set[str] = set()
        candidates: list[RoutingCandidate] = []
        estimated_tokens = self._estimate_tokens(task)
        for model_name in all_models:
            normalized = str(model_name or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            inferred_provider = self._provider_for_model(normalized)
            health_row = self._find_model_health_row(inferred_provider, normalized, model_health)
            provider_name = str(health_row.get("provider") or inferred_provider) if isinstance(health_row, dict) else inferred_provider
            provider_visible = normalized in visible.get(provider_name, set()) or normalized in visible.get(self._display_provider(provider_name), set())
            visible_flag = bool(health_row.get("visible")) if isinstance(health_row, dict) else provider_visible
            workable = bool(health_row.get("workable")) if isinstance(health_row, dict) else False
            routable = bool(health_row.get("routable")) if isinstance(health_row, dict) else False
            reasons: list[str] = []
            if isinstance(health_row, dict):
                status = str(health_row.get("status") or "").strip()
                failure_reason = str(health_row.get("failure_reason") or "").strip()
                source = str(health_row.get("source_of_truth") or "model_health").strip()
                if status:
                    reasons.append(f"status:{status}")
                if failure_reason:
                    reasons.append(f"failure:{failure_reason}")
                if source:
                    reasons.append(f"source:{source}")
            if provider_name == "openai" and not isinstance(health_row, dict):
                workable = self.openai_router.is_runtime_routable_model(normalized, require_allowlist=False)
                routable = self.openai_router.is_runtime_routable_model(normalized, require_allowlist=True) and ModelRoutingPolicy.is_model_available(normalized)
                reasons.extend(openai_reasons)
            elif provider_name != "openai" and not isinstance(health_row, dict):
                workable = provider_visible or provider_name in {"local", "local_llm", "ai_kernel"}
                routable = workable and ModelRoutingPolicy.is_model_available(normalized)
                reasons.append("provider_snapshot" if provider_visible else "static_local_fallback")
            else:
                routable = routable and ModelRoutingPolicy.is_model_available(normalized)
            learned_score, learned_samples = self._learned_weight(task, normalized)
            budget = self.model_usage.evaluate_model_budget(normalized, planned_tokens=estimated_tokens)
            budget_action = str(budget.get("action") or "ok")
            estimated_cost = float(health_row.get("estimated_cost_usd_per_2k") or 0.0) * max(1.0, estimated_tokens / 2000.0) if isinstance(health_row, dict) else ModelRoutingPolicy.estimate_cost_usd(estimated_tokens, normalized)
            score = 0.0
            if visible_flag:
                score += 0.20
            if workable:
                score += 0.25
            if routable:
                score += 0.25
            if normalized in preferred_names:
                score += 0.10
            score += min(0.20, learned_score * 0.20)
            if provider_name in {"local_llm", "ai_kernel"}:
                score += 0.05
            if budget_action == "warn":
                score -= 0.05
            elif budget_action == "reduce":
                score -= 0.10
            elif budget_action == "error":
                score -= 0.25
            candidates.append(
                RoutingCandidate(
                    model_name=normalized,
                    provider=self._display_provider(provider_name),
                    role=role,
                    visible=visible_flag,
                    workable=workable,
                    routable=routable,
                    preferred=normalized in preferred_names,
                    score=round(score, 4),
                    learned_score=round(learned_score, 4),
                    learned_samples=learned_samples,
                    budget_action=budget_action,
                    estimated_cost_usd=estimated_cost,
                    reasons=reasons,
                )
            )
        return sorted(
            candidates,
            key=lambda item: (
                0 if item.routable else 1,
                0 if item.preferred else 1,
                -item.score,
                item.estimated_cost_usd,
                item.model_name,
            ),
        )

    def build_parallel_assignments(self, task: Task, candidates: list[RoutingCandidate]) -> list[ParallelWorkAssignment]:
        files = list(task.input.files)
        if task.type not in {TaskType.CODE, TaskType.FIX, TaskType.TEST, TaskType.REVIEW}:
            return []
        requested_parallelism = self._safe_parallelism(task)
        parallelism = requested_parallelism if files else 1
        if self.model_usage.should_reduce_parallelism():
            parallelism = max(1, parallelism - 1)
        groups = self._group_files(files, max(1, parallelism))
        routable = [candidate for candidate in candidates if candidate.routable]
        if not routable:
            return []
        assignments: list[ParallelWorkAssignment] = []
        for index, file_targets in enumerate(groups):
            lane_kind = self._lane_kind(index, len(groups))
            choice = routable[min(index, len(routable) - 1)]
            assignments.append(
                ParallelWorkAssignment(
                    lane_index=index,
                    lane_kind=lane_kind,
                    worker_role=self._lane_role(task, lane_kind),
                    target_capability="review" if lane_kind == "integration" and task.type == TaskType.CODE else task.type.value,
                    file_targets=list(file_targets),
                    model_name=choice.model_name,
                    provider=choice.provider,
                    routing_hints={
                        "route_mode": "adaptive",
                        "worker_role": self._lane_role(task, lane_kind),
                        "parallel_branch_index": index,
                        "parallel_branch_total": len(groups),
                        "parallel_model": choice.model_name,
                        "parallel_provider": choice.provider,
                        "parallel_lane_kind": lane_kind,
                        "parallel_file_targets": list(file_targets),
                    },
                )
            )
        return assignments

    def decide(self, task: Task, advisory_context: dict[str, Any] | None = None) -> AdaptiveRoutingDecision | None:
        complexity = self._task_complexity(task)
        role = self._task_role(task, complexity)
        candidates = self.build_candidates(task, advisory_context)
        if not candidates:
            return None
        primary = next((candidate for candidate in candidates if candidate.routable), candidates[0])
        fallbacks = [candidate.model_name for candidate in candidates if candidate.model_name != primary.model_name][:4]
        parallel_assignments = self.build_parallel_assignments(task, candidates)
        health_payload = self._model_health_payload()
        selected_health = self._find_model_health_row(primary.provider, primary.model_name, health_payload) or {}
        trace = {
            "route_mode": "adaptive",
            "role": role,
            "primary_model": primary.model_name,
            "primary_provider": primary.provider,
            "candidate_count": len(candidates),
            "visible_models": sum(1 for candidate in candidates if candidate.visible),
            "workable_models": sum(1 for candidate in candidates if candidate.workable),
            "routable_models": sum(1 for candidate in candidates if candidate.routable),
            "fallback_models": list(fallbacks),
            "parallel_assignments": len(parallel_assignments),
            "model_health_generated_at": health_payload.get("generated_at"),
            "model_health_summary": health_payload.get("summary", {}),
            "role_models": self._role_models_from_health(health_payload, role),
            "selected_model_health": selected_health,
        }
        decision = AdaptiveRoutingDecision(
            primary_model=primary.model_name,
            primary_provider=primary.provider,
            role=role,
            complexity=complexity,
            candidates=candidates,
            fallback_models=fallbacks,
            parallel_assignments=parallel_assignments,
            trace=trace,
        )
        self._publish_event("routing.decision.made", {"task_id": task.task_id, "session_id": task.session_id, **trace})
        return decision

    def _publish_event(self, topic: str, payload: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(topic, payload)
        except Exception:
            return
