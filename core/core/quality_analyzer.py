from __future__ import annotations

from .models import AgentResult, QualityReport, Task, TaskStatus, TaskType
from .security import SecurityManager


class QualityAnalyzer:
    def __init__(self, security: SecurityManager | None = None, minimum_confidence: float = 0.7) -> None:
        self.security = security or SecurityManager()
        self.minimum_confidence = minimum_confidence

    @staticmethod
    def _evidence_text(result: AgentResult) -> str:
        output = result.output
        parts = [
            str(output.get("summary", "")),
            str(output.get("diff", "")),
            "\n".join(str(item) for item in output.get("commands_run", []) or []),
            "\n".join(str(item) for item in result.errors or []),
            "\n".join(str(item) for item in output.get("test_results", []) or []),
        ]
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _criterion_hit(criterion: str, evidence_text: str) -> bool:
        needle = str(criterion or "").strip().lower()
        if not needle:
            return False
        haystack = evidence_text.lower()
        tokens = [token for token in needle.replace("/", " ").replace("_", " ").split() if len(token) > 2]
        if needle in haystack:
            return True
        return any(token in haystack for token in tokens)

    def analyze(self, task: Task, result: AgentResult) -> QualityReport:
        issues: list[str] = []
        truth_basis: list[str] = []
        output = result.output
        evidence_text = self._evidence_text(result)
        if result.status != TaskStatus.DONE:
            issues.append("result_status_not_done")
        else:
            truth_basis.append("result_status_done")
        if result.confidence < self.minimum_confidence:
            issues.append("low_confidence")
        if self.security.redact_secrets(evidence_text) != evidence_text:
            issues.append("possible_secret_leakage")
        if not output.get("summary"):
            issues.append("missing_summary")
        else:
            truth_basis.append("summary_present")
        if output.get("commands_run"):
            truth_basis.append("commands_run_present")
        if output.get("test_results"):
            truth_basis.append("test_results_present")
        if output.get("diff"):
            truth_basis.append("diff_present")
        code_like_task = task.type in {TaskType.CODE, TaskType.TEST, TaskType.FIX}
        if code_like_task and task.input.acceptance_criteria:
            matched = [criterion for criterion in task.input.acceptance_criteria if self._criterion_hit(criterion, evidence_text)]
            if matched:
                truth_basis.extend(f"acceptance_matched:{criterion}" for criterion in matched)
            missing = [criterion for criterion in task.input.acceptance_criteria if criterion not in matched]
            if missing:
                issues.append("acceptance_not_proven")
        if code_like_task and not (output.get("test_results") or output.get("commands_run")):
            issues.append("missing_verification_evidence")
        if code_like_task and not output.get("diff"):
            issues.append("missing_diff_evidence")
        if code_like_task and task.input.acceptance_criteria and result.status == TaskStatus.DONE and result.confidence < 0.8:
            issues.append("acceptance_needs_review")
        score = max(0.0, result.confidence - len(issues) * 0.12 + min(0.2, len(truth_basis) * 0.04))
        passed = not issues and (result.status == TaskStatus.DONE)
        return QualityReport(
            passed=passed,
            score=score,
            issues=issues,
            requires_review=not passed,
            evidence={
                "summary": bool(output.get("summary")),
                "commands_run": list(output.get("commands_run", []) or []),
                "test_results": list(output.get("test_results", []) or []),
                "diff_present": bool(output.get("diff")),
            },
            truth_basis=truth_basis,
        )
