from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from .kernel_api import KernelAPI
from .models import Task, TaskType


@dataclass(slots=True)
class ValidationMemoryGate:
    name: str = "validation_memory_gate"
    _api: KernelAPI | None = None
    warmups_total: int = 0
    snapshots_total: int = 0
    conflict_total: int = 0
    consensus_total: int = 0
    last_snapshot: dict[str, Any] = field(default_factory=dict)

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        if self._api:
            self._api.log("info", "[VALIDATION_GATE] loaded")

    def on_unload(self) -> None:
        self._api = None

    def _session_memory(self):
        return self._api.get_context("session_memory") if self._api else None

    def _vfs(self):
        return self._api.get_context("unified_vfs") if self._api else None

    def _task_label(self, task: Task) -> str:
        task_type = getattr(task, "type", None)
        return str(getattr(task_type, "value", task_type) or "unknown")

    def _task_type(self, task: Task) -> str:
        return self._task_label(task).lower()

    @staticmethod
    def _token_overlap_score(left: str, right: str) -> float:
        left_tokens = set(re.findall(r"[a-z0-9_./:#-]+", str(left).lower()))
        right_tokens = set(re.findall(r"[a-z0-9_./:#-]+", str(right).lower()))
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    @staticmethod
    def _trained_memory_trusted(brief: str, memory_domain: str, task: Task) -> bool:
        if not brief or len(brief) < 80:
            return False
        if f"{task.type.value}" not in memory_domain:
            return False
        return "Quality:" in brief

    def _training_domain(self, task: Task) -> str:
        mapping = {
            "plan": "prompt:plan",
            "review": "prompt:review",
            "test": "prompt:test",
            "code": "prompt:code",
            "docs": "prompt:docs",
            "research": "prompt:research",
        }
        return mapping.get(self._task_type(task), f"prompt:{self._task_type(task)}")

    def _project_name(self, task: Task) -> str:
        project = str(getattr(getattr(task, "context", None), "project", "") or "").strip()
        if project:
            return project
        repo_path = str(getattr(getattr(task, "context", None), "repo_path", "") or "").strip()
        return Path(repo_path).name if repo_path else ""

    def _warm_memory(self, task: Task, agent_id: str, memory_domain: str) -> dict[str, Any]:
        session_memory = self._session_memory()
        if not session_memory or not hasattr(session_memory, "hybrid"):
            return {"warmed_session_records": 0, "warmed_trained_records": 0, "fast_hit_count": 0, "warmed_keys": []}

        hybrid = session_memory.hybrid
        warmed_session_records = 0
        warmed_trained_records = 0
        warmed_keys: list[str] = []

        if hasattr(hybrid, "warmup_from_persistent"):
            try:
                warmup = dict(
                    hybrid.warmup_from_persistent(
                        session_id=task.session_id or task.task_id,
                        agent_id=agent_id,
                        memory_domain=memory_domain,
                        top_k=6 if task.type in {TaskType.PLAN, TaskType.REVIEW, TaskType.TEST} else 4,
                        trained_top_k=4,
                    )
                )
                warmed_session_records = int(warmup.get("warmed_session_records", 0) or 0)
                warmed_trained_records = int(warmup.get("warmed_trained_records", 0) or 0)
                warmed_keys = [str(item) for item in warmup.get("warmed_keys", []) if str(item).strip()]
            except Exception:
                pass

        fast_hits = []
        if hasattr(hybrid, "fast_retrieve"):
            try:
                fast_hits = hybrid.fast_retrieve(
                    query_text=str(task.input.description or ""),
                    session_id=task.session_id or None,
                    project_name=self._project_name(task) or None,
                    top_k=3,
                )
            except Exception:
                fast_hits = []

        warmup_result = {
            "session_id": task.session_id or task.task_id,
            "agent_id": agent_id,
            "memory_domain": memory_domain,
            "warmed_session_records": warmed_session_records,
            "warmed_trained_records": warmed_trained_records,
            "fast_hit_count": len(fast_hits),
            "fast_hit_keys": [str(hit.key) for hit in fast_hits],
            "fast_hit_brief": "",
            "warmed_keys": warmed_keys,
        }
        if fast_hits and hasattr(hybrid, "build_context_brief"):
            try:
                warmup_result["fast_hit_brief"] = hybrid.build_context_brief(hits=fast_hits, token_limit=180)
            except Exception:
                pass

        self.warmups_total += warmed_session_records + warmed_trained_records
        return warmup_result

    def _trained_context(self, task: Task, agent_id: str) -> dict[str, Any]:
        session_memory = self._session_memory()
        if not session_memory or not hasattr(session_memory, "hybrid"):
            return {"brief": "", "has_trained_memory": False, "trusted": False, "reason": "memory_unavailable"}

        hybrid = session_memory.hybrid
        domain = self._training_domain(task)
        brief = ""
        ctx: dict[str, Any] = {}

        if hasattr(hybrid, "get_trained_memory_context"):
            try:
                ctx = dict(
                    hybrid.get_trained_memory_context(
                        session_id=task.session_id or task.task_id,
                        agent_id=agent_id,
                        memory_domain=domain,
                        top_k=2 if task.type in {TaskType.PLAN, TaskType.REVIEW, TaskType.TEST} else 1,
                        query_text=str(task.input.description or ""),
                        files=[str(item).strip() for item in list(task.input.files or []) if str(item).strip()],
                        constraints=[str(item).strip() for item in list(task.input.constraints or []) if str(item).strip()],
                        acceptance_criteria=[str(item).strip() for item in list(task.input.acceptance_criteria or []) if str(item).strip()],
                    )
                )
                brief = str(ctx.get("brief") or "").strip()
            except Exception:
                ctx = {}

        if not brief and hasattr(hybrid, "retrieve_trained_memory_brief"):
            try:
                brief = str(
                    hybrid.retrieve_trained_memory_brief(
                        session_id=task.session_id or task.task_id,
                        agent_id=agent_id,
                        memory_domain=domain,
                        top_k=1,
                        token_limit=180 if task.type in {TaskType.PLAN, TaskType.REVIEW, TaskType.TEST} else 120,
                        task_type=self._task_type(task),
                        allow_trained_memory=True,
                        query_text=str(task.input.description or ""),
                        files=[str(item).strip() for item in list(task.input.files or []) if str(item).strip()],
                        constraints=[str(item).strip() for item in list(task.input.constraints or []) if str(item).strip()],
                        acceptance_criteria=[str(item).strip() for item in list(task.input.acceptance_criteria or []) if str(item).strip()],
                    )
                ).strip()
            except Exception:
                brief = ""

        trusted = self._trained_memory_trusted(brief, str(ctx.get("memory_domain") or domain), task)
        provenance = list(ctx.get("provenance") or [])
        if not provenance and "[Sources:" in brief:
            provenance = ["brief_sources"]
        confidence = float(ctx.get("confidence_score", 0.0) or 0.0)
        if confidence <= 0.0 and brief:
            confidence = 1.0

        return {
            "brief": brief,
            "memory_domain": str(ctx.get("memory_domain") or domain),
            "session_id": task.session_id or task.task_id,
            "agent_id": agent_id,
            "has_trained_memory": bool(brief),
            "provenance": provenance,
            "confidence_score": confidence,
            "trusted": trusted,
            "reason": "trusted" if trusted else (str(ctx.get("reason") or "format_untrusted") if brief else "not_found"),
        }

    def _reusable_context(self, task: Task) -> dict[str, Any]:
        session_memory = self._session_memory()
        if not session_memory or not hasattr(session_memory, "hybrid"):
            return {"matched": False, "brief": "", "similarity": 0.0, "reason": "memory_unavailable"}

        hybrid = session_memory.hybrid
        capability = str(getattr(task, "required_capability", "") or self._task_label(task))
        try:
            reusable = dict(
                hybrid.retrieve_reusable_task_context(
                    task=task,
                    agent_id=f"shared:{capability}",
                    capability=capability,
                    top_k=2 if task.type in {TaskType.CODE, TaskType.REVIEW, TaskType.TEST} else 1,
                    token_limit=220,
                )
            )
        except Exception:
            reusable = {"matched": False, "brief": "", "similarity": 0.0, "reason": "reuse_lookup_failed"}

        brief = str(reusable.get("brief") or "").strip()
        return {
            "matched": bool(reusable.get("matched")),
            "brief": brief,
            "similarity": float(reusable.get("similarity", 0.0) or 0.0),
            "fingerprint": str(reusable.get("fingerprint") or ""),
            "count": int(reusable.get("count", 0) or 0),
            "source_ids": list(reusable.get("source_ids") or []),
            "reason": str(reusable.get("reason") or ("trusted" if brief else "reuse_lookup_failed")),
        }

    def _layered_context(self, task: Task, *, agent_id: str, provider: str = "", model_name: str = "") -> dict[str, Any]:
        session_memory = self._session_memory()
        layered = getattr(session_memory, "layered", None) if session_memory else None
        if not layered or not hasattr(layered, "build_context_pie"):
            return {"layered_context_brief": "", "prompt_guidance": []}
        try:
            pie = layered.build_context_pie(task, agent_id=agent_id, provider=provider, model_name=model_name, token_limit=240)
            return {
                "layered_context_brief": str(getattr(pie, "layered_context_brief", "") or ""),
                "prompt_guidance": [str(item).strip() for item in getattr(pie, "prompt_guidance", []) or [] if str(item).strip()],
                "prompt_memory_brief": str(getattr(pie, "prompt_memory_brief", "") or ""),
                "routing_memory_brief": str(getattr(pie, "routing_memory_brief", "") or ""),
                "execution_memory_brief": str(getattr(pie, "execution_memory_brief", "") or ""),
            }
        except Exception:
            return {"layered_context_brief": "", "prompt_guidance": []}

    def _consensus(self, trained: dict[str, Any], reusable: dict[str, Any], layered: dict[str, Any], warmup: dict[str, Any]) -> dict[str, Any]:
        evidence_sources: list[str] = []
        if trained.get("brief") and trained.get("trusted"):
            evidence_sources.append("trained_memory")
        if reusable.get("matched") and reusable.get("brief"):
            evidence_sources.append("reusable_memory")
        if layered.get("layered_context_brief") or layered.get("prompt_guidance"):
            evidence_sources.append("layered_context")
        if warmup.get("fast_hit_count", 0):
            evidence_sources.append("fast_memory")

        conflict_reasons: list[str] = []
        if trained.get("brief") and not trained.get("trusted"):
            conflict_reasons.append(f"trained_memory_untrusted:{trained.get('reason') or 'unknown'}")
        if reusable.get("brief") and not reusable.get("matched"):
            conflict_reasons.append("reusable_memory_unmatched")
        if trained.get("brief") and reusable.get("brief"):
            overlap = self._token_overlap_score(str(trained.get("brief") or ""), str(reusable.get("brief") or ""))
            if overlap < 0.12:
                conflict_reasons.append(f"memory_disagreement:{overlap:.2f}")
        if len(evidence_sources) < 2:
            conflict_reasons.append("insufficient_independent_evidence")

        consensus_score = len(evidence_sources) / 4.0
        validation_conflict = bool(conflict_reasons)
        if validation_conflict:
            consensus_score = max(0.0, consensus_score - 0.2)

        return {
            "validation_memory_consensus": round(consensus_score, 3),
            "validation_memory_conflict": validation_conflict,
            "validation_memory_conflict_reasons": conflict_reasons[:6],
            "validation_evidence_sources": evidence_sources,
        }

    def _persist_snapshot(self, task: Task, agent_id: str, provider: str, model_name: str, snapshot: dict[str, Any]) -> bool:
        session_memory = self._session_memory()
        hybrid = getattr(session_memory, "hybrid", None) if session_memory else None
        persistent = getattr(hybrid, "persistent", None) if hybrid else None
        if not persistent or not hasattr(persistent, "store_memory"):
            return False
        try:
            persistent.store_memory(
                session_id=task.session_id or task.task_id,
                agent_id=agent_id,
                memory_type="ctx:validation_gate",
                content=snapshot,
                importance_score=0.95,
                metadata={
                    "kind": "validation_memory_gate",
                    "provider": provider,
                    "model_name": model_name,
                    "task_id": task.task_id,
                    "session_id": task.session_id or task.task_id,
                },
            )
            return True
        except Exception:
            return False

    def _persist_vfs_snapshot(self, task: Task, agent_id: str, snapshot: dict[str, Any]) -> bool:
        vfs = self._vfs()
        if not vfs or not hasattr(vfs, "write_state"):
            return False
        path = f"validation/memory/{task.session_id or 'default'}/{task.task_id}"
        try:
            return bool(vfs.write_state(path, snapshot, agent_id, metadata={"kind": "validation_memory_gate", "task_id": task.task_id}))
        except Exception:
            return False

    def build_validation_context(self, task: Task, *, agent_id: str, provider: str = "", model_name: str = "", context: dict[str, Any] | None = None) -> dict[str, Any]:
        memory_domain = self._training_domain(task)
        warmup = self._warm_memory(task, agent_id, memory_domain)
        trained = self._trained_context(task, agent_id)
        reusable = self._reusable_context(task)
        layered = self._layered_context(task, agent_id=agent_id, provider=provider, model_name=model_name)
        consensus = self._consensus(trained, reusable, layered, warmup)

        session_memory = self._session_memory()
        hybrid = getattr(session_memory, "hybrid", None) if session_memory else None
        fast_hits = []
        if hybrid and hasattr(hybrid, "fast_retrieve"):
            try:
                fast_hits = hybrid.fast_retrieve(
                    query_text=str(task.input.description or ""),
                    session_id=task.session_id or None,
                    project_name=self._project_name(task) or None,
                    top_k=3,
                )
            except Exception:
                fast_hits = []

        snapshot: dict[str, Any] = {
            "task_id": task.task_id,
            "session_id": task.session_id or task.task_id,
            "agent_id": agent_id,
            "provider": provider,
            "model_name": model_name,
            "warmup": warmup,
            "trained": trained,
            "reusable": reusable,
            "layered": layered,
            "fast_memory_hit_count": len(fast_hits),
            "fast_memory_hit_keys": [str(hit.key) for hit in fast_hits],
            "fast_memory_brief": hybrid.build_context_brief(hits=fast_hits, token_limit=180) if fast_hits and hasattr(hybrid, "build_context_brief") else "",
            **consensus,
            "trained_memory_domain": str(trained.get("memory_domain") or ""),
            "trained_memory_brief": str(trained.get("brief") or ""),
            "trained_memory_trusted": bool(trained.get("trusted")),
            "trained_memory_reason": str(trained.get("reason") or ""),
            "reusable_task_memory_brief": str(reusable.get("brief") or ""),
            "reusable_task_memory_similarity": float(reusable.get("similarity", 0.0) or 0.0),
            "reusable_task_memory_fingerprint": str(reusable.get("fingerprint") or ""),
            "reusable_task_memory_count": int(reusable.get("count", 0) or 0),
            "layered_context_brief": str(layered.get("layered_context_brief") or ""),
            "prompt_guidance": list(layered.get("prompt_guidance") or []),
            "prompt_memory_brief": str(layered.get("prompt_memory_brief") or ""),
            "routing_memory_brief": str(layered.get("routing_memory_brief") or ""),
            "execution_memory_brief": str(layered.get("execution_memory_brief") or ""),
            "validation_snapshot_stored": False,
            "validation_vfs_stored": False,
        }
        snapshot["validation_snapshot_stored"] = self._persist_snapshot(task, agent_id, provider, model_name, snapshot)
        snapshot["validation_vfs_stored"] = self._persist_vfs_snapshot(task, agent_id, snapshot)
        if snapshot["validation_memory_conflict"]:
            self.conflict_total += 1
        self.consensus_total += 1
        self.snapshots_total += 1
        self.last_snapshot = dict(snapshot)
        return snapshot

    def build_runtime_context(self, task: Task, *, agent_id: str, provider: str = "", model_name: str = "", context: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.build_validation_context(task, agent_id=agent_id, provider=provider, model_name=model_name, context=context)

    def before_task(self, task: Task, context: dict[str, Any]) -> None:
        provider = str(context.get("selected_provider") or context.get("provider") or "")
        model_name = str(context.get("selected_model") or context.get("model") or "")
        agent_id = str(getattr(task, "required_capability", "") or self._task_label(task))
        snapshot = self.build_validation_context(task, agent_id=agent_id, provider=provider, model_name=model_name, context=context)
        context["validation_memory_gate"] = snapshot
        context["validation_memory_consensus"] = snapshot.get("validation_memory_consensus", 0.0)
        context["validation_memory_conflict"] = snapshot.get("validation_memory_conflict", False)
        context["validation_memory_conflict_reasons"] = list(snapshot.get("validation_memory_conflict_reasons") or [])
        context["validation_vfs_path"] = f"validation/memory/{task.session_id or 'default'}/{task.task_id}"
        context["validation_vfs_integrity_ok"] = bool(snapshot.get("validation_vfs_stored"))

    def finalize(self) -> dict[str, Any]:
        return {
            "warmups_total": self.warmups_total,
            "snapshots_total": self.snapshots_total,
            "conflict_total": self.conflict_total,
            "consensus_total": self.consensus_total,
            "last_snapshot": dict(self.last_snapshot),
        }
