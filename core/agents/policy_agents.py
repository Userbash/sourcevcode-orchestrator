
from __future__ import annotations

from typing import Any

from core.agents.base_agent import BaseAgent
from core.core.availability import ProviderStatus
from core.core.models import (
    ActivationResult,
    AgentResult,
    ApprovalResult,
    DeprecationResult,
    HandoffPayload,
    PolicyDecision,
    RemovalResult,
    RuleChangeProposal,
    SimulationReport,
    Task,
    TaskStatus,
)


class ExecutionAgent(BaseAgent):
    def supported_protocols(self) -> list[str]:
        return ["task_execution/v1", "handoff/v1", "execution_evidence/v1"]


class PolicyAgent(BaseAgent):
    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        decision = self.evaluate(task, memory_context or {})
        summary = "; ".join(decision.reasons) or decision.decision
        return self.result(task, summary, status=TaskStatus.DONE, confidence=decision.confidence)

    def supported_protocols(self) -> list[str]:
        return ["policy_decision/v1", "handoff/v1"]


class GovernanceAgent(PolicyAgent):
    def __init__(self, agent_id: str, capabilities: list[str], *, policy_version: str = "builtin/v1") -> None:
        super().__init__(agent_id, capabilities)
        self.policy_version = policy_version
        self._rules: dict[str, dict[str, Any]] = {}
        self._active_versions: list[str] = [policy_version]

    def propose_rule_change(self, change: RuleChangeProposal | dict[str, Any]) -> RuleChangeProposal:
        if isinstance(change, RuleChangeProposal):
            proposal = change
        else:
            proposal = RuleChangeProposal(**change)
        self._rules[proposal.proposal_id] = proposal.as_dict()
        return proposal

    def approve_rule_change(self, proposal_id: str, approver: str) -> ApprovalResult:
        if proposal_id not in self._rules:
            return ApprovalResult(approved=False, approver=approver, reasons=["proposal_not_found"])
        return ApprovalResult(approved=True, approver=approver, reasons=["approved"])

    def activate_rule_set(self, version: str) -> ActivationResult:
        if version not in self._active_versions:
            self._active_versions.append(version)
        self.policy_version = version
        return ActivationResult(activated=True, version=version, reasons=["activated"])

    def deprecate_rule(self, rule_id: str) -> DeprecationResult:
        return DeprecationResult(deprecated=rule_id in self._rules, rule_id=rule_id, reasons=["deprecated"] if rule_id in self._rules else ["rule_not_found"])

    def remove_rule(self, rule_id: str) -> RemovalResult:
        if rule_id in self._rules:
            self._rules.pop(rule_id, None)
            return RemovalResult(removed=True, rule_id=rule_id, reasons=["removed"])
        return RemovalResult(removed=False, rule_id=rule_id, reasons=["rule_not_found"])


class PlannerPolicyAgent(PolicyAgent):
    def __init__(self, agent_id: str = "planner_policy_agent") -> None:
        super().__init__(agent_id, ["planner", "decompose", "acceptance_design"])
        self.set_identity(provider="local", model_name="planner-policy")

    def evaluate(self, task: Task, context: dict | None = None) -> PolicyDecision:
        execution_mode = "parallel" if bool((task.routing_hints or {}).get("parallelize_code")) else "serial"
        dependency_count = len(task.dependencies or [])
        return PolicyDecision(
            decision="PLAN_READY",
            severity="info",
            reasons=[f"execution_mode:{execution_mode}", f"dependency_count:{dependency_count}"],
            evidence={
                "task_contract": {
                    "acceptance_criteria": list(task.input.acceptance_criteria or []),
                    "constraints": list(task.input.constraints or []),
                    "files": list(task.input.files or []),
                    "execution_mode": execution_mode,
                    "dependency_count": dependency_count,
                }
            },
            policy_version="planner/v1",
            confidence=0.8,
            next_action="route",
            agent_id=self.agent_id,
        )


class SecurityPolicyAgent(PolicyAgent):
    def __init__(self, agent_id: str = "security_agent") -> None:
        super().__init__(agent_id, ["security", "policy_guard", "secret_scan"])
        self.set_identity(provider="local", model_name="security-policy")

    def evaluate(self, task: Task, context: dict | None = None) -> PolicyDecision:
        text = " ".join([
            task.input.description,
            " ".join(task.input.constraints or []),
            " ".join(task.input.files or []),
        ]).lower()
        risk_flags = []
        for token in ("secret", "token", "password", "ssh", "deploy", "release", "rm -rf", "network", "curl ", "wget "):
            if token in text:
                risk_flags.append(token.strip())
        decision = "ALLOW"
        next_action = "route"
        severity = "info"
        if any(flag in {"rm -rf", "password", "secret", "token"} for flag in risk_flags):
            decision = "DENY"
            next_action = "block"
            severity = "high"
        elif risk_flags:
            decision = "REVIEW"
            next_action = "route"
            severity = "medium"
        return PolicyDecision(
            decision=decision,
            severity=severity,
            reasons=risk_flags or ["no_high_risk_signals"],
            evidence={"risk_flags": risk_flags, "task_type": task.type.value},
            policy_version="security/v1",
            confidence=0.9,
            next_action=next_action,
            agent_id=self.agent_id,
        )


class RoutingPolicyAgent(PolicyAgent):
    def __init__(self, agent_id: str = "routing_policy_agent") -> None:
        super().__init__(agent_id, ["routing", "provider_policy", "budget_policy"])
        self.set_identity(provider="local", model_name="routing-policy")

    def evaluate(self, task: Task, context: dict | None = None) -> PolicyDecision:
        context = context or {}
        capability = str(context.get("capability") or task.required_capability or task.type.value)
        preferred_agent = (task.routing_hints or {}).get("preferred_agent_id")
        registry = context.get("registry")
        candidates: list[str] = []
        if registry is not None and hasattr(registry, "list_agents"):
            try:
                for record in registry.list_agents():
                    if capability in list(getattr(record, "capabilities", []) or []):
                        candidates.append(str(getattr(record, "id", "")))
            except Exception:
                candidates = []
        if preferred_agent and preferred_agent in candidates:
            candidates = [preferred_agent] + [item for item in candidates if item != preferred_agent]
        decision = "ALLOW" if candidates else "DENY"
        reasons = [f"capability:{capability}"]
        if preferred_agent:
            reasons.append(f"preferred_agent:{preferred_agent}")
        if not candidates:
            reasons.append("no_candidates")
        return PolicyDecision(
            decision=decision,
            severity="info" if candidates else "high",
            reasons=reasons,
            evidence={"capability": capability, "candidate_agents": candidates, "preferred_agent": preferred_agent},
            policy_version="routing/v1",
            confidence=0.85 if candidates else 1.0,
            next_action="execute" if candidates else "block",
            agent_id=self.agent_id,
        )


class ProviderReadinessAgent(PolicyAgent):
    def __init__(self, agent_id: str = "provider_ops_agent") -> None:
        super().__init__(agent_id, ["provider_health", "readiness", "capacity_signal"])
        self.set_identity(provider="local", model_name="provider-readiness")

    def evaluate(self, task: Task, context: dict | None = None) -> PolicyDecision:
        context = context or {}
        availability = context.get("availability")
        provider = str(context.get("provider") or "local")
        live = bool(context.get("live", False))
        status_value = "unknown"
        error = None
        if availability is not None and hasattr(availability, "check_provider"):
            try:
                health = availability.check_provider(provider, live=live)
                status_value = health.status.value
                error = health.error
            except Exception as exc:
                status_value = "failed"
                error = str(exc)
        ready = status_value in {ProviderStatus.HEALTHY.value, ProviderStatus.DEGRADED.value, "unknown"}
        return PolicyDecision(
            decision="ALLOW" if ready else "DENY",
            severity="info" if ready else "high",
            reasons=[f"provider:{provider}", f"status:{status_value}"] + ([str(error)] if error else []),
            evidence={"provider": provider, "status": status_value, "error": error},
            policy_version="provider-readiness/v1",
            confidence=0.9,
            next_action="execute" if ready else "fallback",
            agent_id=self.agent_id,
        )


class ReviewPolicyAgent(PolicyAgent):
    def __init__(self, agent_id: str = "review_agent") -> None:
        super().__init__(agent_id, ["review", "quality_gate"])
        self.set_identity(provider="local", model_name="review-policy")

    def evaluate(self, task: Task, context: dict | None = None) -> PolicyDecision:
        context = context or {}
        result = context.get("result")
        quality = context.get("quality")
        reasons: list[str] = []
        evidence: dict[str, Any] = {}
        decision = "PASS"
        next_action = "complete"
        if result is not None:
            commands = list(result.output.get("commands_run", []) or [])
            tests = list(result.output.get("test_results", []) or [])
            evidence["commands_run"] = commands
            evidence["test_results"] = tests
            if task.type.value in {"code", "fix", "test"} and not tests:
                reasons.append("missing_test_evidence")
                decision = "NEEDS_REVIEW"
                next_action = "review"
        if quality is not None:
            evidence["quality_score"] = getattr(quality, "score", 0.0)
            evidence["quality_passed"] = getattr(quality, "passed", False)
            if not getattr(quality, "passed", False):
                reasons.extend(list(getattr(quality, "issues", []) or ["quality_gate_failed"]))
                decision = "FAIL"
                next_action = "fix"
        if result is not None and result.status == TaskStatus.FAILED:
            decision = "FAIL"
            next_action = "fix"
            reasons.extend(list(result.errors or ["execution_failed"]))
        if not reasons:
            reasons.append("review_passed")
        return PolicyDecision(
            decision=decision,
            severity="info" if decision == "PASS" else "medium",
            reasons=reasons,
            evidence=evidence,
            policy_version="review/v1",
            confidence=0.85,
            next_action=next_action,
            agent_id=self.agent_id,
        )


class FixPolicyAgent(PolicyAgent):
    def __init__(self, agent_id: str = "fix_agent") -> None:
        super().__init__(agent_id, ["repair", "retry_strategy"])
        self.set_identity(provider="local", model_name="fix-policy")

    def evaluate(self, task: Task, context: dict | None = None) -> PolicyDecision:
        context = context or {}
        result = context.get("result")
        review = context.get("review_decision")
        retry_limit = int(context.get("retry_limit", 0) or 0)
        retry_count = int(getattr(task, "retry_count", 0) or 0)
        needs_fix = False
        reasons: list[str] = []
        if result is not None and result.status == TaskStatus.FAILED:
            needs_fix = retry_count < retry_limit
            reasons.extend(list(result.errors or ["execution_failed"]))
        if review is not None and getattr(review, "decision", "") in {"FAIL", "NEEDS_REVIEW"}:
            needs_fix = needs_fix or (retry_count < retry_limit)
            reasons.extend(list(getattr(review, "reasons", []) or []))
        decision = "CREATE_FIX_TASK" if needs_fix else "COMPLETE"
        return PolicyDecision(
            decision=decision,
            severity="medium" if needs_fix else "info",
            reasons=reasons or ["no_fix_required"],
            evidence={"retry_count": retry_count, "retry_limit": retry_limit},
            policy_version="fix/v1",
            confidence=0.9,
            next_action="retry" if needs_fix else "complete",
            agent_id=self.agent_id,
        )


class MemoryHandoffAgent(PolicyAgent):
    def __init__(self, agent_id: str = "memory_handoff_agent") -> None:
        super().__init__(agent_id, ["memory", "handoff", "context_compression"])
        self.set_identity(provider="local", model_name="memory-handoff")

    def build_handoff(self, task: Task, result: AgentResult, target_agent: str) -> HandoffPayload:
        output = result.output
        return HandoffPayload(
            from_agent=result.agent_id,
            to_agent=target_agent,
            task_id=task.task_id,
            summary=str(output.get("summary", "") or ""),
            artifacts=list(output.get("files_changed", []) or []),
            errors=list(result.errors or []),
            risk_flags=list(output.get("warnings", []) or []),
            evidence_refs=list(output.get("commands_run", []) or []),
            upstream_policy_versions=list(output.get("upstream_policy_versions", []) or []),
        )

    def evaluate(self, task: Task, context: dict | None = None) -> PolicyDecision:
        return PolicyDecision(
            decision="HANDOFF_READY",
            reasons=["typed_handoff_enforced"],
            evidence={"schema_version": "1.0"},
            policy_version="handoff/v1",
            confidence=1.0,
            next_action="handoff",
            agent_id=self.agent_id,
        )


class RuleGovernanceAgent(GovernanceAgent):
    def __init__(self, agent_id: str = "rule_governance_agent") -> None:
        super().__init__(agent_id, ["rule_admin", "policy_versioning", "policy_audit"], policy_version="governance/v1")
        self.set_identity(provider="local", model_name="rule-governance")

    def evaluate(self, task: Task, context: dict | None = None) -> PolicyDecision:
        return PolicyDecision(
            decision="POLICY_SNAPSHOT_READY",
            reasons=["governance_snapshot_available"],
            evidence={"policy_version": self.policy_version, "active_versions": list(self._active_versions)},
            policy_version=self.policy_version,
            confidence=1.0,
            next_action="route",
            agent_id=self.agent_id,
        )

    def simulate(self, rule_change: RuleChangeProposal | dict, sample_tasks: list[Task] | None = None) -> SimulationReport:
        proposal = rule_change if isinstance(rule_change, RuleChangeProposal) else RuleChangeProposal(**rule_change)
        return SimulationReport(
            simulated=True,
            policy_version=self.policy_version,
            sample_size=len(sample_tasks or []),
            findings=[f"proposal:{proposal.proposal_id}", f"domain:{proposal.domain}"],
            metrics={"sample_size": len(sample_tasks or []), "approvals_required": 3},
        )
