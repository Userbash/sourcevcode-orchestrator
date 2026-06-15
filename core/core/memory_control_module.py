from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .kernel_api import KernelAPI
from .models import AgentResult, ExecutionPlan, Task, TaskType


def _normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    aliases = {
        "google": "antigravity",
        "agy": "antigravity",
        "antigravity-cli": "antigravity",
        "mino": "mimo",
    }
    return aliases.get(value, value)


@dataclass(slots=True)
class MemoryControlModule:
    name: str = "memory_control"
    _api: KernelAPI | None = None
    writes_total: int = 0
    reads_total: int = 0
    applied_total: int = 0
    parallel_batches_total: int = 0
    last_parallel_plan: dict[str, dict[str, Any]] = field(default_factory=dict)
    profile_stats: dict[str, int] = field(default_factory=dict)

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._api.log("info", "[MEMORY_CONTROL] loaded")

    def on_unload(self) -> None:
        self._api = None

    def _layered(self):
        return self._api.get_context("layered_context_memory") if self._api else None

    def _session_memory(self):
        return self._api.get_context("session_memory") if self._api else None

    def _config(self):
        return self._api.get_context("orchestration_config") if self._api else None

    @staticmethod
    def _task_type(task: Task) -> str:
        return str(getattr(getattr(task, "type", None), "value", getattr(task, "type", "unknown")) or "unknown").lower()

    @staticmethod
    def _training_domain(task: Task) -> str:
        task_type = MemoryControlModule._task_type(task)
        return {
            "plan": "prompt:plan",
            "review": "prompt:review",
            "test": "prompt:test",
            "code": "prompt:code",
            "docs": "prompt:docs",
            "research": "prompt:research",
        }.get(task_type, f"prompt:{task_type}")

    @staticmethod
    def _scope_identifier(task: Task, agent_id: str) -> tuple[str, str]:
        scope_name = (task.memory_scope or "task").lower()
        if scope_name == "session":
            return "session", task.session_id or "default"
        if scope_name == "agent":
            return "agent", agent_id
        if scope_name == "capability":
            return "capability", task.required_capability or task.type.value.lower()
        return "task", task.task_id

    @staticmethod
    def _profile_for_provider(provider: str, model_name: str) -> str:
        provider_norm = _normalize_provider(provider)
        model_norm = str(model_name or "").strip().lower()
        if provider_norm == "antigravity":
            return "rich_synthesis"
        if provider_norm == "mistral":
            return "focused_execution"
        if provider_norm == "local":
            return "drafting"
        if provider_norm == "mimo" or "mimo" in model_norm or "mino" in model_norm:
            return "routing_meta"
        return "balanced"

    def _record_profile(self, profile: str) -> None:
        self.profile_stats[profile] = int(self.profile_stats.get(profile, 0) or 0) + 1

    def register_submission(self, task: Task, *, raw_payload: Any, normalized_payload: dict[str, Any], source: str) -> None:
        layered = self._layered()
        if layered and hasattr(layered, "record_submission"):
            layered.record_submission(task, raw_payload=raw_payload, normalized_payload=normalized_payload, source=source)
            self.writes_total += 2

    def register_planning_draft(self, task: Task, advisory_context: dict[str, Any] | None, *, source: str = "orchestrator") -> None:
        layered = self._layered()
        if layered and hasattr(layered, "record_planning_draft"):
            layered.record_planning_draft(task, advisory_context, source=source)
            self.writes_total += 1

    def register_decomposition(self, task: Task, plan: ExecutionPlan, *, source: str) -> None:
        layered = self._layered()
        if layered and hasattr(layered, "record_decomposition"):
            layered.record_decomposition(task, plan, source=source)
            self.writes_total += 1

    def register_routing_outcome(
        self,
        task: Task,
        *,
        selected_provider: str,
        selected_model: str,
        routed_agent: str,
        routed_provider: str,
        routed_model: str,
        reason: str,
        fallback_count: int,
    ) -> None:
        layered = self._layered()
        if layered and hasattr(layered, "record_routing_outcome"):
            layered.record_routing_outcome(
                task,
                selected_provider=selected_provider,
                selected_model=selected_model,
                routed_agent=routed_agent,
                routed_provider=routed_provider,
                routed_model=routed_model,
                reason=reason,
                fallback_count=fallback_count,
            )
            self.writes_total += 1

    def register_result(self, task: Task, result: AgentResult, *, quality_score: float, fallback_count: int, latency_ms: float) -> None:
        layered = self._layered()
        if layered and hasattr(layered, "record_result"):
            layered.record_result(
                task,
                result,
                quality_score=quality_score,
                fallback_count=fallback_count,
                latency_ms=latency_ms,
            )
            self.writes_total += 3

    def _base_context(self, task: Task, agent_id: str) -> dict[str, Any]:
        memory = self._session_memory()
        if not memory:
            return {}
        scope, identifier = self._scope_identifier(task, agent_id)
        context: dict[str, Any] = {}
        if task.cache_policy == "write_only":
            return context
        for key in task.memory_keys:
            normalized = key.lower()
            if "thought" in normalized or normalized.endswith(":errors") or normalized == "errors":
                continue
            value = memory.get(scope, identifier, key)
            if value is not None:
                context[key] = value
        return context

    def _trained_memory(self, task: Task, agent_id: str) -> dict[str, Any]:
        memory = self._session_memory()
        if not memory or not hasattr(memory, "hybrid"):
            return {}
        config = self._config()
        high_risk_enabled = bool(getattr(config, "high_risk_trained_memory_enabled", False)) if config else False
        task_type = self._task_type(task)
        if not (high_risk_enabled or task_type in {"plan", "review", "test", "code", "docs", "research"}):
            return {"trained_memory_disabled_for_risk": True}
        token_limit = 180 if task.type in {TaskType.PLAN, TaskType.REVIEW, TaskType.TEST} else 120
        brief = memory.hybrid.retrieve_trained_memory_brief(
            session_id=task.session_id or task.task_id,
            agent_id=agent_id,
            memory_domain=self._training_domain(task),
            top_k=1,
            token_limit=token_limit,
            task_type=task_type,
            allow_trained_memory=high_risk_enabled,
        )
        if not brief:
            return {"trained_memory_disabled_for_risk": not high_risk_enabled}
        return {
            "trained_memory_domain": self._training_domain(task),
            "trained_memory_brief": brief,
            "trained_memory_trusted": len(brief) >= 80 and "Quality:" in brief,
            "trained_memory_disabled_for_risk": False,
        }

    def _reusable_memory(self, task: Task) -> dict[str, Any]:
        memory = self._session_memory()
        if not memory or not hasattr(memory, "hybrid"):
            return {}
        capability = task.required_capability or task.type.value.lower()
        reusable = memory.hybrid.retrieve_reusable_task_context(
            task=task,
            agent_id=f"shared:{capability}",
            capability=capability,
            top_k=2 if task.type in {TaskType.CODE, TaskType.REVIEW, TaskType.TEST} else 1,
            token_limit=160 if task.type in {TaskType.PLAN, TaskType.REVIEW, TaskType.TEST} else 140,
        )
        if not reusable.get("matched") or not str(reusable.get("brief") or "").strip():
            return {}
        return {
            "reusable_task_memory_brief": str(reusable.get("brief") or ""),
            "reusable_task_memory_similarity": float(reusable.get("similarity", 0.0) or 0.0),
            "reusable_task_memory_fingerprint": str(reusable.get("fingerprint") or ""),
            "reusable_task_memory_count": int(reusable.get("count", 0) or 0),
        }

    @staticmethod
    def _trim_text(value: Any, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    def _apply_profile(self, profile: str, context: dict[str, Any]) -> dict[str, Any]:
        context = dict(context)
        if profile == "rich_synthesis":
            keys = (
                "layered_context_brief",
                "trained_memory_brief",
                "reusable_task_memory_brief",
                "prompt_memory_brief",
                "execution_memory_brief",
                "routing_memory_brief",
                "prompt_guidance",
            )
        elif profile == "focused_execution":
            keys = (
                "layered_context_brief",
                "reusable_task_memory_brief",
                "prompt_guidance",
            )
            if "layered_context_brief" in context:
                context["layered_context_brief"] = self._trim_text(context["layered_context_brief"], 700)
        elif profile == "drafting":
            keys = (
                "layered_context_brief",
                "prompt_memory_brief",
                "prompt_guidance",
                "routing_memory_brief",
            )
            if "layered_context_brief" in context:
                context["layered_context_brief"] = self._trim_text(context["layered_context_brief"], 900)
        elif profile == "routing_meta":
            keys = (
                "routing_memory_brief",
                "execution_memory_brief",
                "trained_memory_brief",
                "prompt_guidance",
            )
        else:
            keys = (
                "layered_context_brief",
                "trained_memory_brief",
                "reusable_task_memory_brief",
                "prompt_guidance",
            )
        filtered = {key: context[key] for key in keys if key in context}
        for passthrough in (
            "trained_memory_domain",
            "trained_memory_trusted",
            "trained_memory_disabled_for_risk",
            "reusable_task_memory_similarity",
            "reusable_task_memory_fingerprint",
            "reusable_task_memory_count",
        ):
            if passthrough in context:
                filtered[passthrough] = context[passthrough]
        filtered["memory_profile"] = profile
        filtered["memory_apply_order"] = list(keys)
        return filtered

    def build_runtime_context(self, task: Task, *, agent_id: str, provider: str = "", model_name: str = "") -> dict[str, Any]:
        context = self._base_context(task, agent_id)
        context.update(self._trained_memory(task, agent_id))
        context.update(self._reusable_memory(task))
        layered = self._layered()
        if layered and hasattr(layered, "build_context_pie"):
            pie = layered.build_context_pie(task, agent_id=agent_id, provider=provider, model_name=model_name)
            if getattr(pie, "layered_context_brief", ""):
                context["layered_context_brief"] = pie.layered_context_brief
            if getattr(pie, "prompt_memory_brief", ""):
                context["prompt_memory_brief"] = pie.prompt_memory_brief
            if getattr(pie, "routing_memory_brief", ""):
                context["routing_memory_brief"] = pie.routing_memory_brief
            if getattr(pie, "execution_memory_brief", ""):
                context["execution_memory_brief"] = pie.execution_memory_brief
            if getattr(pie, "prompt_guidance", None):
                context["prompt_guidance"] = list(pie.prompt_guidance)
        profile = self._profile_for_provider(provider, model_name)
        self._record_profile(profile)
        self.reads_total += 1
        self.applied_total += 1
        return self._apply_profile(profile, context)

    def prepare_parallel_batch(self, tasks: list[Task], assignments: dict[str, str], registry: Any | None = None) -> dict[str, dict[str, Any]]:
        batch_plan: dict[str, dict[str, Any]] = {}
        family = "-".join(sorted(task.task_id[:8] for task in tasks))
        for index, task in enumerate(tasks):
            agent_id = str(assignments.get(task.task_id) or "")
            record = registry.get(agent_id) if registry and agent_id else None
            provider = str(getattr(record, "provider", "") or "")
            model_name = str(getattr(record, "model_name", "") or "")
            profile = self._profile_for_provider(provider, model_name)
            payload = {
                "agent_id": agent_id,
                "provider": provider,
                "model_name": model_name,
                "memory_profile": profile,
                "parallel_family": family,
                "branch_index": index,
                "task_type": self._task_type(task),
            }
            task.routing_hints = dict(task.routing_hints or {})
            task.routing_hints["memory_profile"] = profile
            task.routing_hints["parallel_family"] = family
            task.routing_hints["parallel_branch_index"] = index
            batch_plan[task.task_id] = payload
            self._record_profile(profile)
        self.parallel_batches_total += 1
        self.last_parallel_plan = batch_plan
        return batch_plan

    def finalize(self) -> dict[str, Any]:
        return {
            "writes_total": self.writes_total,
            "reads_total": self.reads_total,
            "applied_total": self.applied_total,
            "parallel_batches_total": self.parallel_batches_total,
            "profile_stats": dict(self.profile_stats),
            "last_parallel_plan": dict(self.last_parallel_plan),
        }
