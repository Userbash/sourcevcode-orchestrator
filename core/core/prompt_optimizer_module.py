from __future__ import annotations

import logging
import re
from typing import Any

from .external_ai_bridge import ExternalAIBridge
from .kernel_protocol import KernelAPI
from .models import AgentResult, Task, TaskType

logger = logging.getLogger("prompt_optimizer")


class PromptOptimizerModule:
    name: str = "prompt_optimizer"
    _api: KernelAPI | None = None

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._api.log("info", f"[OPTIMIZER] {self.name} module loaded.")

    def on_unload(self) -> None:
        pass

    def _memory_history(self, task: Task) -> list[dict[str, Any]]:
        if not self._api:
            return []
        memory = self._api.get_context("session_memory")
        if not memory or not hasattr(memory, "hybrid"):
            return []
        session_id = task.session_id or "default"
        try:
            history = memory.hybrid.get_command_history(session_id=session_id, limit=3)
            return history if isinstance(history, list) else []
        except Exception:
            return []

    def _memory_decisions(self, task: Task) -> list[str]:
        if not self._api:
            return []
        memory = self._api.get_context("session_memory")
        if not memory or not hasattr(memory, "hybrid"):
            return []
        session_id = task.session_id or "default"
        fetchers = []
        for name in ("get_decision_history", "get_relevant_decisions", "get_recent_decisions"):
            fn = getattr(memory.hybrid, name, None)
            if callable(fn):
                fetchers.append(fn)
        decisions: list[str] = []
        for fn in fetchers:
            try:
                values = fn(session_id=session_id, limit=5)
            except TypeError:
                try:
                    values = fn(session_id=session_id)
                except Exception:
                    continue
            except Exception:
                continue
            decisions.extend(self._normalize_lines(values))
            if decisions:
                break
        return decisions[:5]

    @staticmethod
    def _safe_offload_types() -> set[TaskType]:
        return {TaskType.PLAN, TaskType.DOCS, TaskType.RESEARCH, TaskType.REVIEW, TaskType.TEST, TaskType.CODE, TaskType.FIX}

    @staticmethod
    def _normalize_lines(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, tuple):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        if not text:
            return []
        return [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]

    @staticmethod
    def _normalized_text_profile(task: Task) -> dict[str, Any]:
        hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
        profile = hints.get("normalized_text_profile")
        return profile if isinstance(profile, dict) else {}

    @staticmethod
    def _task_hints(task: Task) -> dict[str, Any]:
        return task.routing_hints if isinstance(task.routing_hints, dict) else {}

    @classmethod
    def _frame_package(cls, task: Task) -> dict[str, Any]:
        package = cls._task_hints(task).get("frame_orchestrator")
        return package if isinstance(package, dict) else {}

    @classmethod
    def _frame_xml_package(cls, task: Task) -> str:
        xml = cls._task_hints(task).get("frame_xml_package")
        return str(xml).strip() if isinstance(xml, str) else ""

    @classmethod
    def _uses_internal_chat_ingress(cls, task: Task) -> bool:
        hints = cls._task_hints(task)
        source = str(hints.get("source") or "").strip().lower()
        ingress = str(hints.get("ingress_path") or "").strip().lower()
        return source == "websocket" or ingress == "websocket_internal_chat" or bool(hints.get("external_chat"))

    @staticmethod
    def _task_type_label(task: Task) -> str:
        task_type = getattr(task, "type", None)
        return str(getattr(task_type, "value", task_type) or "unknown")

    @staticmethod
    def _trained_memory_domain_for_task(task: Task) -> str:
        task_type = str(getattr(task.type, "value", task.type) or "unknown").lower()
        return {
            "plan": "prompt:plan",
            "review": "prompt:review",
            "test": "prompt:test",
            "code": "prompt:code",
            "docs": "prompt:docs",
            "research": "prompt:research",
        }.get(task_type, f"prompt:{task_type}")

    @staticmethod
    def _memory_token_budget(task: Task) -> int:
        if task.type in {TaskType.PLAN, TaskType.REVIEW, TaskType.TEST}:
            return 240
        if task.type in {TaskType.CODE, TaskType.FIX}:
            return 220
        if task.type in {TaskType.DOCS, TaskType.RESEARCH}:
            return 180
        return 160

    @staticmethod
    def _trained_memory_trusted(brief: str, memory_domain: str, task: Task) -> bool:
        if not brief or len(brief) < 80:
            return False
        if f"{task.type.value}" not in memory_domain:
            return False
        if "Quality:" not in brief:
            return False
        return True

    def _task_quality_threshold(self, task: Task) -> float:
        if not self._api:
            return 0.75
        config = self._api.get_context("orchestration_config")
        if not config:
            return 0.75
        thresholds = getattr(config, "trained_memory_quality_thresholds_by_task", {}) or {}
        key = str(task.type.value).lower()
        return float(thresholds.get(key, getattr(config, "trained_memory_quality_threshold", 0.75)) or 0.75)

    @staticmethod
    def _trained_memory_policy(context: dict[str, Any] | None) -> dict[str, Any]:
        policy = context.get("trained_memory_policy") if isinstance(context, dict) else {}
        policy = policy if isinstance(policy, dict) else {}
        return {
            "allow_injection": bool(policy.get("allow_injection", True)),
            "allowed_domains": {str(item) for item in (policy.get("allowed_domains") or []) if str(item).strip()},
            "denied_domains": {str(item) for item in (policy.get("denied_domains") or []) if str(item).strip()},
            "max_age_sec": int(policy.get("max_age_sec") or 604800),
        }

    def _trained_memory_validation_reason(self, ctx: dict[str, Any], brief: str, memory_domain: str, task: Task, policy: dict[str, Any]) -> str:
        if not policy.get("allow_injection", True):
            return "policy_denied"
        allowed_domains = policy.get("allowed_domains") or set()
        if allowed_domains and memory_domain not in allowed_domains:
            return "domain_not_allowed"
        denied_domains = policy.get("denied_domains") or set()
        if memory_domain in denied_domains:
            return "domain_denied"
        if not self._trained_memory_trusted(brief, memory_domain, task):
            return "format_untrusted"
        provenance = ctx.get("provenance") or ctx.get("sources") or ctx.get("source_ids") or []
        if not provenance and "[Sources:" in brief:
            provenance = ["brief_sources"]
        if not provenance:
            return "missing_provenance"
        confidence = ctx.get("confidence_score")
        if confidence is not None and float(confidence) < self._task_quality_threshold(task):
            return "low_confidence"
        age_sec = ctx.get("age_sec")
        if age_sec is not None and float(age_sec) > float(policy.get("max_age_sec") or 604800):
            return "stale_memory"
        return "trusted"

    def _trained_memory_context(self, task: Task, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._api:
            return {"brief": "", "has_trained_memory": False, "trusted": False, "reason": "api_unavailable"}
        if self._is_high_risk_task(task):
            self._record_trained_memory_outcome(task, accepted=False, reason="high_risk_disabled")
            return {"brief": "", "has_trained_memory": False, "trusted": False, "disabled_for_risk": True, "reason": "high_risk_disabled"}
        policy = self._trained_memory_policy(context)
        memory = self._api.get_context("session_memory")
        if not memory or not hasattr(memory, "hybrid"):
            return {"brief": "", "has_trained_memory": False, "trusted": False, "reason": "memory_unavailable"}
        hybrid = memory.hybrid
        session_id = task.session_id or "default"
        agent_id = task.assigned_model or task.required_capability or self._task_type_label(task)
        domain = self._trained_memory_domain_for_task(task)
        top_k = 2 if task.type in {TaskType.PLAN, TaskType.REVIEW, TaskType.TEST} else 1
        try:
            if hasattr(hybrid, "get_trained_memory_context"):
                call_kwargs = {
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "memory_domain": domain,
                    "top_k": top_k,
                    "query_text": str(task.input.description or ""),
                    "files": [str(item).strip() for item in list(task.input.files or []) if str(item).strip()],
                    "constraints": [str(item).strip() for item in list(task.input.constraints or []) if str(item).strip()],
                    "acceptance_criteria": [str(item).strip() for item in list(task.input.acceptance_criteria or []) if str(item).strip()],
                }
                try:
                    ctx = hybrid.get_trained_memory_context(**call_kwargs)
                except TypeError:
                    ctx = hybrid.get_trained_memory_context(
                        session_id=session_id,
                        agent_id=agent_id,
                        memory_domain=domain,
                        top_k=top_k,
                    )
                brief = str(ctx.get("brief") or "").strip()
                memory_domain = str(ctx.get("memory_domain") or domain)
                reason = self._trained_memory_validation_reason(ctx, brief, memory_domain, task, policy)
                ctx["trusted"] = reason == "trusted"
                ctx["reason"] = reason
                self._record_trained_memory_outcome(task, accepted=bool(ctx.get("trusted")), reason=reason)
                return ctx
        except Exception:
            pass
            if hasattr(hybrid, "retrieve_trained_memory_brief"):
                brief = hybrid.retrieve_trained_memory_brief(
                    session_id=session_id,
                    agent_id=agent_id,
                    memory_domain=domain,
                    top_k=top_k,
                    token_limit=self._memory_token_budget(task),
                    query_text=str(task.input.description or ''),
                    files=[str(item).strip() for item in list(task.input.files or []) if str(item).strip()],
                    constraints=[str(item).strip() for item in list(task.input.constraints or []) if str(item).strip()],
                    acceptance_criteria=[str(item).strip() for item in list(task.input.acceptance_criteria or []) if str(item).strip()],
                )
                ctx = {
                    "brief": brief,
                    "memory_domain": domain,
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "has_trained_memory": bool(brief),
                    "provenance": ["brief_cache"] if brief else [],
                    "confidence_score": 1.0 if brief else 0.0,
                }
                reason = self._trained_memory_validation_reason(ctx, brief, domain, task, policy)
                ctx["trusted"] = reason == "trusted"
                ctx["reason"] = reason
                self._record_trained_memory_outcome(task, accepted=ctx["trusted"], reason=reason)
                return ctx
        except Exception:
            pass
        return {"brief": "", "has_trained_memory": False, "trusted": False, "reason": "not_found"}

    def apply_trained_memory(self, task: Task, base_instruction: str, trained: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> str:
        trained = trained or self._trained_memory_context(task, context)
        brief = str(trained.get("brief") or "").strip()
        if not brief or not trained.get("trusted"):
            self._record_trained_memory_outcome(task, accepted=False, reason=str(trained.get("reason") or "not_trusted"))
            return base_instruction
        return "\n".join([base_instruction, "TRAINED MEMORY:", brief])

    def _extract_objective(self, task: Task) -> str:
        raw = str(task.input.description or "").strip()
        if not raw:
            return "No explicit objective provided."
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            return "No explicit objective provided."
        objective = lines[0]
        if len(lines) > 1:
            objective = f"{objective} | details: {'; '.join(lines[1:4])}"
        if len(objective) > 320:
            objective = objective[:320].rstrip() + "..."
        return objective

    def _layered_context_memory(self, task: Task, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._api:
            return {}
        layered = self._api.get_context("layered_context_memory")
        if not layered or not hasattr(layered, "build_context_pie"):
            return {}
        try:
            provider = str((context or {}).get("selected_provider") or (context or {}).get("provider") or "")
            model_name = str((context or {}).get("selected_model") or (context or {}).get("model") or getattr(task, "assigned_model", "") or "")
            pie = layered.build_context_pie(
                task,
                agent_id=str(getattr(task, "required_capability", "") or self._task_type_label(task)),
                provider=provider,
                model_name=model_name,
                token_limit=max(120, self._memory_token_budget(task)),
            )
            return {
                "layered_context_brief": str(getattr(pie, "layered_context_brief", "") or ""),
                "prompt_guidance": list(getattr(pie, "prompt_guidance", []) or []),
                "prompt_memory_brief": str(getattr(pie, "prompt_memory_brief", "") or ""),
            }
        except Exception:
            return {}

    def _reusable_memory_context(self, task: Task, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._api:
            return {"matched": False, "brief": "", "similarity": 0.0, "reason": "api_unavailable"}
        memory = self._api.get_context("session_memory")
        if not memory or not hasattr(memory, "hybrid"):
            return {"matched": False, "brief": "", "similarity": 0.0, "reason": "memory_unavailable"}
        hybrid = memory.hybrid
        if not hasattr(hybrid, "retrieve_reusable_task_context"):
            return {"matched": False, "brief": "", "similarity": 0.0, "reason": "reuse_unsupported"}
        capability = str(getattr(task, "required_capability", "") or self._task_type_label(task))
        try:
            return hybrid.retrieve_reusable_task_context(
                task=task,
                agent_id=f"shared:{capability}",
                capability=capability,
                top_k=2 if task.type in {TaskType.CODE, TaskType.REVIEW, TaskType.TEST} else 1,
                token_limit=self._memory_token_budget(task),
            )
        except Exception:
            return {"matched": False, "brief": "", "similarity": 0.0, "reason": "reuse_lookup_failed"}

    @staticmethod
    def _token_overlap_score(left: str, right: str) -> float:
        left_tokens = set(re.findall(r"[a-z0-9_./:#-]+", str(left).lower()))
        right_tokens = set(re.findall(r"[a-z0-9_./:#-]+", str(right).lower()))
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    def _memory_consensus_context(
        self,
        task: Task,
        trained: dict[str, Any] | None,
        reusable: dict[str, Any] | None,
        layered: dict[str, Any] | None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gate = self._api.get_context("validation_memory_gate") if self._api else None
        if gate and hasattr(gate, "build_validation_context"):
            try:
                provider = str((context or {}).get("selected_provider") or (context or {}).get("provider") or "")
                model_name = str((context or {}).get("selected_model") or (context or {}).get("model") or getattr(task, "assigned_model", "") or "")
                agent_id = str(getattr(task, "required_capability", "") or self._task_type_label(task))
                gate_context = gate.build_validation_context(task, agent_id=agent_id, provider=provider, model_name=model_name, context=context)
                if isinstance(gate_context, dict) and gate_context.get("validation_memory_consensus") is not None:
                    return {
                        "validation_memory_consensus": float(gate_context.get("validation_memory_consensus", 0.0) or 0.0),
                        "validation_memory_conflict": bool(gate_context.get("validation_memory_conflict")),
                        "validation_memory_conflict_reasons": list(gate_context.get("validation_memory_conflict_reasons") or []),
                        "validation_vfs_path": str(gate_context.get("validation_vfs_path") or f"validation/memory/{task.session_id or 'default'}/{task.task_id}"),
                        "validation_vfs_integrity_ok": bool(gate_context.get("validation_vfs_stored")),
                    }
            except Exception:
                pass
        trained = trained or {}
        reusable = reusable or {}
        layered = layered or {}
        context = context or {}
        trained_brief = str(trained.get("brief") or "").strip()
        reusable_brief = str(reusable.get("brief") or reusable.get("reusable_task_memory_brief") or "").strip()
        layered_brief = str(layered.get("layered_context_brief") or "").strip()
        prompt_guidance = [str(item).strip() for item in (layered.get("prompt_guidance") or context.get("prompt_guidance") or []) if str(item).strip()]

        evidence_sources = []
        if trained_brief and trained.get("trusted"):
            evidence_sources.append("trained_memory")
        if reusable_brief and reusable.get("matched"):
            evidence_sources.append("reusable_memory")
        if layered_brief or prompt_guidance:
            evidence_sources.append("layered_context")

        conflict_reasons: list[str] = []
        if trained_brief and not trained.get("trusted"):
            conflict_reasons.append(f"trained_memory_untrusted:{trained.get('reason') or 'unknown'}")
        if reusable_brief and not reusable.get("matched"):
            conflict_reasons.append("reusable_memory_unmatched")
        if trained_brief and reusable_brief:
            overlap = self._token_overlap_score(trained_brief, reusable_brief)
            if overlap < 0.12:
                conflict_reasons.append(f"memory_disagreement:{overlap:.2f}")

        consensus_score = len(evidence_sources) / 3.0
        validation_conflict = bool(conflict_reasons) or len(evidence_sources) < 2
        if len(evidence_sources) < 2:
            conflict_reasons.append("insufficient_independent_evidence")

        vfs = self._api.get_context("unified_vfs") if self._api else None
        vfs_path = f"validation/prompt/{task.session_id or 'default'}/{task.task_id}"
        vfs_integrity_ok = False
        if vfs and hasattr(vfs, "write_state") and hasattr(vfs, "read_state"):
            snapshot = {
                "task_id": task.task_id,
                "session_id": task.session_id or "default",
                "trained_memory_domain": str(trained.get("memory_domain") or ""),
                "trained_memory_trusted": bool(trained.get("trusted")),
                "trained_memory_reason": str(trained.get("reason") or ""),
                "reusable_task_memory_similarity": float(reusable.get("similarity", 0.0) or reusable.get("reusable_task_memory_similarity", 0.0) or 0.0),
                "layered_context_present": bool(layered_brief),
                "prompt_guidance_count": len(prompt_guidance),
                "consensus_score": round(consensus_score, 3),
                "validation_conflict": validation_conflict,
                "conflict_reasons": list(conflict_reasons),
            }
            try:
                write_ok = bool(vfs.write_state(vfs_path, snapshot, str(task.assigned_model or self._task_type_label(task)), metadata={"kind": "prompt_optimizer_validation", "task_id": task.task_id}))
                node = vfs.read_state(vfs_path)
                vfs_integrity_ok = bool(write_ok and node is not None and getattr(node, "content", None) == snapshot)
            except Exception:
                vfs_integrity_ok = False
        else:
            conflict_reasons.append("vfs_unavailable")

        if not vfs_integrity_ok:
            conflict_reasons.append("vfs_integrity_unverified")
            validation_conflict = True

        if validation_conflict:
            consensus_score = max(0.0, consensus_score - 0.25)

        return {
            "validation_memory_consensus": round(consensus_score, 3),
            "validation_memory_conflict": validation_conflict,
            "validation_memory_conflict_reasons": conflict_reasons[:6],
            "validation_vfs_path": vfs_path,
            "validation_vfs_integrity_ok": vfs_integrity_ok,
        }

    def _extract_context(self, task: Task, history: list[dict[str, Any]], offload: dict[str, Any] | None, trained: dict[str, Any] | None = None, reusable: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> list[str]:
        context_lines: list[str] = []
        if task.session_id:
            context_lines.append(f"session_id: {task.session_id}")
        if task.context.repo_path:
            context_lines.append(f"repo_path: {task.context.repo_path}")
        if task.context.branch:
            context_lines.append(f"branch: {task.context.branch}")
        if task.input.files:
            context_lines.append(f"files: {', '.join(task.input.files)}")
        if task.input.constraints:
            context_lines.append(f"constraints: {', '.join(task.input.constraints)}")
        if task.input.acceptance_criteria:
            context_lines.append(f"acceptance_criteria: {', '.join(task.input.acceptance_criteria)}")
        hints = self._task_hints(task)
        if self._uses_internal_chat_ingress(task):
            context_lines.append(f"ingress_path: {str(hints.get('ingress_path') or 'websocket_internal_chat')}")
            context_lines.append(f"text_preparation_mode: {str(hints.get('text_preparation_mode') or 'automatic')}")
        frame_package = self._frame_package(task)
        if frame_package:
            validation = frame_package.get("validation") if isinstance(frame_package.get("validation"), dict) else {}
            semantic_gap = frame_package.get("semantic_gap") if isinstance(frame_package.get("semantic_gap"), dict) else {}
            roles = validation.get("worker_roles") if isinstance(validation.get("worker_roles"), list) else []
            role_names = [str(item.get("role") or "").strip() for item in roles if isinstance(item, dict) and str(item.get("role") or "").strip()]
            gaps = [str(item).strip() for item in (semantic_gap.get("gap_scanner") or []) if str(item).strip()]
            if role_names:
                context_lines.append(f"frame_worker_roles: {', '.join(role_names)}")
            if gaps:
                context_lines.append(f"frame_semantic_gaps: {', '.join(gaps[:6])}")
            context_lines.append(f"frame_contract_status: {str(frame_package.get('status') or 'validated')}")
        profile = self._normalized_text_profile(task)
        if profile:
            context_lines.append(
                "normalized_profile: "
                f"intent={profile.get('intent_bucket', '')}; "
                f"risk={profile.get('risk_bucket', '')}; "
                f"scope={profile.get('scope_bucket', '')}; "
                f"execution={profile.get('execution_shape', '')}; "
                f"quality={profile.get('input_quality_bucket', '')}; "
                f"trust={profile.get('decision_trust', '')}; "
                f"confidence={profile.get('confidence_score', 0.0)}"
            )
            reasons = [str(item).strip() for item in (profile.get('reasons') or []) if str(item).strip()]
            for item in reasons[:3]:
                context_lines.append(f"normalized_reason: {item}")
        if history:
            context_lines.append(f"recent_successful_history_items: {len(history)}")
        trained = trained or self._trained_memory_context(task, context)
        reusable = reusable or self._reusable_memory_context(task, context)
        brief = str(trained.get("brief") or "").strip()
        if brief and trained.get("trusted"):
            context_lines.append(f"trained_memory_domain: {trained.get('memory_domain', '')}")
            context_lines.append(f"trained_memory_brief: {brief[:240]}")
        if reusable.get("matched") and str(reusable.get("brief") or "").strip():
            context_lines.append(f"reusable_task_similarity: {float(reusable.get("similarity", 0.0) or 0.0):.2f}")
            context_lines.append(f"reusable_task_fingerprint: {reusable.get("fingerprint", "")}")
            context_lines.append(f"reusable_task_memory_brief: {str(reusable.get("brief") or "")[:240]}")
        layered = self._layered_context_memory(task, context)
        consensus = self._memory_consensus_context(task, trained, reusable, layered, context)
        context_lines.append(f"validation_memory_consensus: {consensus['validation_memory_consensus']:.3f}")
        context_lines.append(f"validation_memory_conflict: {consensus['validation_memory_conflict']}")
        if consensus["validation_memory_conflict_reasons"]:
            context_lines.append(f"validation_memory_conflict_reasons: {', '.join(consensus['validation_memory_conflict_reasons'])}")
        context_lines.append(f"validation_vfs_path: {consensus['validation_vfs_path']}")
        context_lines.append(f"validation_vfs_integrity_ok: {consensus['validation_vfs_integrity_ok']}")
        decisions = self._memory_decisions(task)
        if decisions:
            context_lines.append(f"memory_decisions: {len(decisions)}")
            context_lines.extend(f"memory_decision: {item}" for item in decisions[:3])
        if offload:
            summary = str(offload.get("summary") or "").strip()
            if summary:
                context_lines.append(f"local_llm_summary: {summary[:320]}")
            next_steps = self._normalize_lines(offload.get("next_steps"))
            if next_steps:
                context_lines.append(f"local_llm_next_steps: {', '.join(next_steps[:5])}")
            if isinstance(offload.get("analysis"), dict):
                analysis = offload.get("analysis") or {}
                tags = self._normalize_lines(analysis.get("tags"))
                if tags:
                    context_lines.append(f"analysis_tags: {', '.join(tags[:8])}")
        return context_lines

    def _extract_requirements(self, task: Task, offload: dict[str, Any] | None) -> list[str]:
        requirements: list[str] = []
        if task.input.constraints:
            requirements.extend(f"must: {item}" for item in self._normalize_lines(task.input.constraints))
        if task.input.acceptance_criteria:
            requirements.extend(f"acceptance: {item}" for item in self._normalize_lines(task.input.acceptance_criteria))
        if task.input.files:
            requirements.append(f"inspect_files: {', '.join(task.input.files[:8])}")
        if offload:
            actions = self._normalize_lines(offload.get("actions"))
            if actions:
                requirements.extend(f"analysis_step: {step}" for step in actions[:5])
            offload_policy = offload.get("offload") if isinstance(offload.get("offload"), dict) else {}
            if isinstance(offload_policy, dict):
                core_only = self._normalize_lines(offload_policy.get("core_only"))
                if core_only:
                    requirements.extend(f"core_boundary: {step}" for step in core_only[:5])
        profile = self._normalized_text_profile(task)
        if str(profile.get("execution_shape") or "") == "parallel_candidate":
            requirements.append("parallelize only independent branches and keep a final consolidation step.")
        if str(profile.get("decision_trust") or "") == "rough_hint":
            requirements.append("validate intent and scope before acting because intake quantization confidence is limited.")
        if self._uses_internal_chat_ingress(task):
            requirements.append("treat websocket/internal chat ingress as the authoritative upstream transport and preserve automatic text preparation.")
        if self._frame_xml_package(task):
            requirements.append("use the embedded frame_xml_package as the authoritative orchestration contract before implementation.")
        if not requirements:
            requirements.append("derive explicit requirements from the objective before executing.")
        return requirements

    def _extract_risks(self, task: Task, offload: dict[str, Any] | None) -> list[str]:
        risks: list[str] = []
        text = f"{task.input.description} {' '.join(task.input.constraints)}".lower()
        keywords = {
            "security": "security-sensitive; verify permissions and data exposure.",
            "auth": "authentication/authorization impact; preserve access controls.",
            "rbac": "role and permission changes require strict validation.",
            "migration": "data migration can break existing state; plan rollback and backup.",
            "production": "production-impacting change; keep steps reversible and observable.",
            "destructive": "destructive operation; require explicit confirmation and dry run.",
            "secret": "secret handling must be redacted and never echoed back.",
        }
        for keyword, note in keywords.items():
            if keyword in text:
                risks.append(note)
        if task.priority.value in {"high", "critical"}:
            risks.append("priority is elevated; prefer conservative changes and explicit validation.")
        profile = self._normalized_text_profile(task)
        if str(profile.get("risk_bucket") or "") == "high":
            risks.append("intake quantization marked this request as high-risk; prefer stronger validation and explicit rollback.")
        if str(profile.get("input_quality_bucket") or "") in {"noisy_but_usable", "sparse"}:
            risks.append("input quality is degraded or sparse; restate assumptions before mutating code or state.")
        if offload and offload.get("high_risk"):
            risks.append("local LLM flagged high risk; inspect the prompt before mutating state.")
        if not risks:
            risks.append("no obvious risk markers, but still verify assumptions before implementation.")
        return risks

    def _extract_steps(self, task: Task, history: list[dict[str, Any]], offload: dict[str, Any] | None) -> list[str]:
        steps: list[str] = []
        if offload:
            next_steps = self._normalize_lines(offload.get("next_steps"))
            if next_steps:
                steps.extend(next_steps[:5])
        if task.type == TaskType.CODE or task.type == TaskType.FIX:
            steps.extend([
                "rewrite the request into problem / constraints / plan / tests / rollback sections",
                "identify the exact files and code paths involved",
                "apply minimal code changes with clear boundaries",
                "add or update tests that prove the behavior",
                "define rollback and verification steps before merging",
            ])
        elif task.type == TaskType.PLAN:
            steps.extend([
                "rewrite the request as an execution brief with explicit deliverables",
                "break the task into 3-7 atomic sub-tasks",
                "separate planning, implementation, verification, and documentation",
                "identify dependencies and parallelizable pieces",
            ])
        elif task.type == TaskType.TEST:
            steps.extend([
                "convert the request into a test design brief before writing cases",
                "define the test matrix and failure modes first",
                "cover happy path and regressions",
                "make failures actionable",
            ])
        elif task.type == TaskType.DOCS:
            steps.extend([
                "turn the request into a documentation brief with audience and scope",
                "turn the task into concise but complete documentation",
                "include examples and edge cases",
                "keep terminology consistent with the codebase",
            ])
        elif task.type == TaskType.REVIEW:
            steps.extend([
                "turn the request into a review brief with pass/fail criteria",
                "review correctness, security, and maintainability separately",
                "list concrete issues with severity and file references",
                "recommend only actionable fixes",
            ])
        elif task.type == TaskType.RESEARCH:
            steps.extend([
                "turn the request into a research brief with the exact question to answer",
                "compare options and tradeoffs",
                "summarize findings with source links or code references",
                "end with a clear recommendation",
            ])
        profile = self._normalized_text_profile(task)
        if str(profile.get("execution_shape") or "") == "parallel_candidate":
            steps.append("split the work into independent branches, assign ownership, and keep a final merge/review stage.")
        if str(profile.get("execution_shape") or "") == "single_lane_validation":
            steps.append("run the work through a single validated lane and keep review/test checkpoints explicit.")
        steps.append("treat your first answer as provisional: self-check it, challenge assumptions, and revise conclusions when evidence contradicts them.")
        if history:
            steps.append("reuse only the relevant successful patterns from recent history.")
        if not steps:
            steps.extend([
                "rewrite the task as a detailed implementation brief",
                "split the work into concrete phases",
                "state how success will be verified",
            ])
        return steps

    def _extract_output_contract(self, task: Task) -> list[str]:
        return [
            "return a structured response with clear sections",
            "prefer explicit tasks, dependencies, and validation steps",
            "do not omit important assumptions or risks",
            "keep the output actionable for another AI agent or engineer",
            "if the request is ambiguous, state assumptions explicitly instead of guessing",
        ]

    def _is_high_risk_task(self, task: Task) -> bool:
        if not self._api:
            return False
        config = self._api.get_context("orchestration_config")
        if config and hasattr(config, "should_ask_confirmation"):
            try:
                return bool(config.should_ask_confirmation(task))
            except Exception:
                pass
        selector = self._api.get_context("model_selector")
        if selector and hasattr(selector, "classify"):
            try:
                from .models import Complexity
                complexity = selector.classify(task)
                return complexity in {Complexity.HIGH, Complexity.CRITICAL}
            except Exception:
                return False
        return False

    def _record_trained_memory_outcome(self, task: Task, *, accepted: bool, reason: str) -> None:
        metrics = getattr(self._api, "metrics", None)
        if metrics and hasattr(metrics, "record_trained_memory_outcome"):
            try:
                metrics.record_trained_memory_outcome(task_type=self._task_type_label(task), accepted=accepted, reason=reason)
            except Exception:
                pass
        memory = self._api.get_context("session_memory") if self._api else None
        hybrid = getattr(memory, "hybrid", None) if memory else None
        if hybrid and hasattr(hybrid, "record_trained_memory_outcome"):
            try:
                config = self._api.get_context("orchestration_config") if self._api else None
                threshold = float(getattr(config, "trained_memory_quality_threshold", 0.75) or 0.75)
                hybrid.record_trained_memory_outcome(session_id=task.session_id or "default", task_type=self._task_type_label(task), accepted=accepted, threshold=threshold, reason=reason)
            except Exception:
                pass

    def _render_instruction(self, task: Task, history: list[dict[str, Any]], offload: dict[str, Any] | None, trained: dict[str, Any] | None = None, reusable: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> str:
        objective = self._extract_objective(task)
        context_lines = self._extract_context(task, history, offload, trained=trained, reusable=reusable, context=context)
        requirements = self._extract_requirements(task, offload)
        risks = self._extract_risks(task, offload)
        steps = self._extract_steps(task, history, offload)
        output_contract = self._extract_output_contract(task)

        sections = [
            f"ROLE: You are an expert {self._task_type_label(task)} planner and implementation assistant.",
            f"OBJECTIVE: {objective}",
        ]
        if context_lines:
            sections.append("CONTEXT:")
            sections.extend(f"- {line}" for line in context_lines)
        sections.append("REQUIREMENTS:")
        sections.extend(f"- {item}" for item in requirements)
        sections.append("PLAN:")
        sections.extend(f"- {item}" for item in steps)
        sections.append("RISKS:")
        sections.extend(f"- {item}" for item in risks)
        sections.append("OUTPUT CONTRACT:")
        sections.extend(f"- {item}" for item in output_contract)
        sections.append("FINAL INSTRUCTION:")
        sections.append(
            "Break the request into a detailed, unambiguous execution instruction. "
            "Make hidden assumptions explicit, split complex work into numbered parts, "
            "and optimize for development quality, correctness, and testability. "
            "Do not trust the first conclusion automatically: self-check, look for contradictions, "
            "and revise the answer when the evidence is weak or inconsistent."
        )
        return "\n".join(sections)

    def _local_llm(self, task: Task, context: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not self._api:
            return None
        module_manager = self._api.get_context("module_manager")
        local_llm = module_manager.get_module("local_llm") if module_manager and hasattr(module_manager, "get_module") else None
        if not local_llm or not hasattr(local_llm, "build_offload_profile"):
            return None
        if task.type not in self._safe_offload_types():
            return None
        try:
            return local_llm.build_offload_profile(task, {**context, "memory_hits": history})
        except Exception as exc:
            self._api.log("warning", f"[OPTIMIZER] local_llm offload profile failed: {exc}")
            return None

    def _antigravity_rewrite(self, task: Task, instruction: str) -> str | None:
        if not self._api:
            return None
        host_bridge = self._api.get_context("host_bridge")
        bridge = ExternalAIBridge(host_bridge=host_bridge)
        prompt = (
            "Rewrite the instruction for an orchestrator. Return concise JSON or a structured instruction. "
            "Keep the original meaning, add concrete steps, preserve safety boundaries, and do not invent requirements.\\n\\n"
            f"Original instruction:\\n{instruction}"
        )
        try:
            result = bridge.run_antigravity_cli(task, prompt, timeout_sec=90)
            if result.ok and result.output.strip():
                return result.output.strip()
        except Exception as exc:
            self._api.log("warning", f"[OPTIMIZER] antigravity rewrite failed: {exc}")
        return None

    def _compose_instruction(self, task: Task, history: list[dict[str, Any]], offload: dict[str, Any] | None, context: dict[str, Any] | None = None, trained: dict[str, Any] | None = None, reusable: dict[str, Any] | None = None) -> str:
        trained = trained or self._trained_memory_context(task, context)
        reusable = reusable or self._reusable_memory_context(task, context)
        refined = self._render_instruction(task, history, offload, trained=trained, reusable=reusable, context=context)
        if not self._is_high_risk_task(task):
            refined = self.apply_trained_memory(task, refined, trained=trained, context=context)

        if reusable and reusable.get("matched") and str(reusable.get("brief") or "").strip():
            refined = "\n".join([refined, "REUSABLE TASK MEMORY:", str(reusable.get("brief") or "").strip()])

        if history:
            compact_history = []
            for cmd in history:
                if not cmd.get("success"):
                    continue
                summary = str(cmd.get("result", {}).get("summary", "") or "").strip()
                if summary:
                    compact_history.append(f"- {cmd.get('command')}: {summary[:180]}")
            if compact_history:
                refined = "\n".join([
                    refined,
                    "RELEVANT PRIOR SUCCESSFUL CONTEXT:",
                    *compact_history[:3],
                ])

        if offload:
            safe = offload.get("offload") if isinstance(offload.get("offload"), dict) else {}
            if isinstance(safe, dict):
                full_offload = safe.get("full_offload", [])
                partial_offload = safe.get("partial_offload", [])
                if full_offload or partial_offload:
                    refined = "\n".join([
                        refined,
                        f"OFFLOAD_POLICY: full={full_offload}; partial={partial_offload}",
                    ])
        layered = self._layered_context_memory(task, context)
        layered_brief = str(layered.get("layered_context_brief") or "").strip()
        if layered_brief:
            refined = "\n".join([refined, "LAYERED CONTEXT MEMORY:", layered_brief])
        guidance = [str(item).strip() for item in (layered.get("prompt_guidance") or []) if str(item).strip()]
        if guidance:
            refined = "\n".join([refined, "PROMPT GUIDANCE:", *[f"- {item}" for item in guidance[:6]]])
        frame_package = self._frame_package(task)
        if frame_package:
            validation = frame_package.get("validation") if isinstance(frame_package.get("validation"), dict) else {}
            semantic_gap = frame_package.get("semantic_gap") if isinstance(frame_package.get("semantic_gap"), dict) else {}
            socraticode = frame_package.get("socraticode") if isinstance(frame_package.get("socraticode"), dict) else {}
            socraticode_compaction = frame_package.get("socraticode_context_compaction") if isinstance(frame_package.get("socraticode_context_compaction"), dict) else {}
            frame_lines: list[str] = []
            best_practices = [str(item).strip() for item in (validation.get("best_practices_generation") or []) if str(item).strip()]
            architectural_fixes = [str(item).strip() for item in (validation.get("architectural_fixes") or []) if str(item).strip()]
            gap_scanner = [str(item).strip() for item in (semantic_gap.get("gap_scanner") or []) if str(item).strip()]
            if gap_scanner:
                frame_lines.append("frame_gap_scanner: " + ", ".join(gap_scanner[:8]))
            if best_practices:
                frame_lines.append("frame_best_practices: " + "; ".join(best_practices[:4]))
            if architectural_fixes:
                frame_lines.append("frame_architectural_fixes: " + "; ".join(architectural_fixes[:4]))
            if socraticode:
                status = str(socraticode.get("status") or "").strip()
                score = socraticode.get("coverage_score")
                coverage_status = str(socraticode.get("coverage_status") or "").strip()
                provider = str(socraticode.get("preferred_provider") or "").strip()
                parallel = socraticode.get("recommended_parallel_branches")
                if status:
                    frame_lines.append(f"socraticode_status: {status}")
                if score is not None:
                    frame_lines.append(f"socraticode_coverage: {score} ({coverage_status or 'n/a'})")
                if provider:
                    frame_lines.append(f"socraticode_preferred_provider: {provider}")
                if parallel not in {None, ''}:
                    frame_lines.append(f"socraticode_parallel_branches: {parallel}")
                compact = str(socraticode.get("compact_context_summary") or "").strip()
                if compact:
                    refined = "\n".join([refined, "SOCRATICODE CONTEXT SNAPSHOT:", compact[:700]])
            if socraticode_compaction:
                mode = str(socraticode_compaction.get("compaction_mode") or "").strip()
                strategy = str(socraticode_compaction.get("recommended_prompt_strategy") or "").strip()
                source = str(socraticode_compaction.get("prompt_context_source") or "").strip()
                reduction = str(socraticode_compaction.get("token_reduction_expected") or "").strip()
                raw_allowed = socraticode_compaction.get("raw_file_dump_allowed")
                if mode:
                    frame_lines.append(f"socraticode_compaction_mode: {mode}")
                if source:
                    frame_lines.append(f"socraticode_context_source: {source}")
                if reduction:
                    frame_lines.append(f"socraticode_token_reduction_expected: {reduction}")
                if raw_allowed is not None:
                    frame_lines.append(f"socraticode_raw_file_dump_allowed: {raw_allowed}")
                if strategy:
                    refined = "\n".join([refined, "SOCRATICODE CONTEXT COMPACTION:", strategy])
            if frame_lines:
                refined = "\n".join([refined, "FRAME GUIDANCE:", *[f"- {item}" for item in frame_lines]])
        frame_xml = self._frame_xml_package(task)
        if frame_xml and self._uses_internal_chat_ingress(task):
            refined = "\n".join([refined, "FRAME ORCHESTRATION PACKAGE:", frame_xml])
        return refined

    def before_task(self, task: Task, context: dict[str, Any]) -> None:
        if not self._api:
            return

        history = self._memory_history(task)
        offload = self._local_llm(task, context, history) if history else self._local_llm(task, context, [])
        trained = self._trained_memory_context(task, context)
        reusable = self._reusable_memory_context(task, context)
        instruction = self._compose_instruction(task, history, offload, context, trained, reusable)

        rewritten = None
        if offload and task.type in self._safe_offload_types():
            rewritten = self._antigravity_rewrite(task, instruction)

        final_instruction = rewritten or instruction
        original_description = str(task.input.description or "")
        task.input.description = final_instruction
        if not task.routing_hints:
            task.routing_hints = {}
        task.routing_hints.setdefault("original_description", original_description)
        task.routing_hints["prompt_optimizer"] = {
            "history_items": len(history),
            "local_llm_used": bool(offload),
            "antigravity_used": bool(rewritten),
            "trained_memory_used": bool(trained.get("trusted") and str(trained.get("brief") or "").strip()),
            "trained_memory_reason": str(trained.get("reason") or "not_used"),
            "trained_memory_domain": str(trained.get("memory_domain") or ""),
            "source": "prompt_optimizer",
        }
        task.routing_hints["memory_reuse"] = {
            "matched": bool(reusable.get("matched")),
            "similarity": float(reusable.get("similarity", 0.0) or 0.0),
            "fingerprint": str(reusable.get("fingerprint") or ""),
            "count": int(reusable.get("count", 0) or 0),
            "source_ids": list(reusable.get("source_ids") or []),
            "source": "prompt_optimizer",
        }
        self._api.log(
            "info",
            f"[OPTIMIZER] Prompt prepared: history={len(history)} reusable={bool(reusable.get("matched"))} local_llm={bool(offload)} antigravity={bool(rewritten)}",
        )

    def after_task(self, task: Task, result: AgentResult, context: dict[str, Any]) -> None:
        pass

    def finalize(self) -> dict[str, Any]:
        return {"status": "active"}
