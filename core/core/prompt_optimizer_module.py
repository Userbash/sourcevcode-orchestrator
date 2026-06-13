from __future__ import annotations

import logging
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
        try:
            if hasattr(hybrid, "retrieve_trained_memory_brief"):
                brief = hybrid.retrieve_trained_memory_brief(
                    session_id=session_id,
                    agent_id=agent_id,
                    memory_domain=domain,
                    top_k=top_k,
                    token_limit=self._memory_token_budget(task),
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

    def _extract_context(self, task: Task, history: list[dict[str, Any]], offload: dict[str, Any] | None, trained: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> list[str]:
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
        if history:
            context_lines.append(f"recent_successful_history_items: {len(history)}")
        trained = self._trained_memory_context(task)
        brief = str(trained.get("brief") or "").strip()
        if brief and trained.get("trusted"):
            context_lines.append(f"trained_memory_domain: {trained.get('memory_domain', '')}")
            context_lines.append(f"trained_memory_brief: {brief[:240]}")
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

    def _render_instruction(self, task: Task, history: list[dict[str, Any]], offload: dict[str, Any] | None, trained: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> str:
        objective = self._extract_objective(task)
        context_lines = self._extract_context(task, history, offload, trained=trained, context=context)
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
            "and optimize for development quality, correctness, and testability."
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

    def _compose_instruction(self, task: Task, history: list[dict[str, Any]], offload: dict[str, Any] | None, context: dict[str, Any] | None = None, trained: dict[str, Any] | None = None) -> str:
        trained = trained or self._trained_memory_context(task, context)
        refined = self._render_instruction(task, history, offload, trained=trained, context=context)
        if not self._is_high_risk_task(task):
            refined = self.apply_trained_memory(task, refined, trained=trained, context=context)

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
        return refined

    def before_task(self, task: Task, context: dict[str, Any]) -> None:
        if not self._api:
            return

        history = self._memory_history(task)
        offload = self._local_llm(task, context, history) if history else self._local_llm(task, context, [])
        trained = self._trained_memory_context(task, context)
        instruction = self._compose_instruction(task, history, offload, context, trained)

        rewritten = None
        if offload and task.type in self._safe_offload_types():
            rewritten = self._antigravity_rewrite(task, instruction)

        final_instruction = rewritten or instruction
        task.input.description = final_instruction
        if not task.routing_hints:
            task.routing_hints = {}
        task.routing_hints["prompt_optimizer"] = {
            "history_items": len(history),
            "local_llm_used": bool(offload),
            "antigravity_used": bool(rewritten),
            "trained_memory_used": bool(trained.get("trusted") and str(trained.get("brief") or "").strip()),
            "trained_memory_reason": str(trained.get("reason") or "not_used"),
            "trained_memory_domain": str(trained.get("memory_domain") or ""),
            "source": "prompt_optimizer",
        }
        self._api.log(
            "info",
            f"[OPTIMIZER] Prompt prepared: history={len(history)} local_llm={bool(offload)} antigravity={bool(rewritten)}",
        )

    def after_task(self, task: Task, result: AgentResult, context: dict[str, Any]) -> None:
        pass

    def finalize(self) -> dict[str, Any]:
        return {"status": "active"}
