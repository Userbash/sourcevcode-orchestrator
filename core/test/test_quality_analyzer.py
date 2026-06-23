from core.core.models import AgentResult, ResultOutput, Task, TaskContext, TaskInput, TaskStatus, TaskType
from core.core.quality_analyzer import QualityAnalyzer


def _task(task_type: TaskType = TaskType.CODE) -> Task:
    return Task(task_type, TaskInput("implement guarded truth validation", acceptance_criteria=["tests pass", "diff present"]), TaskContext("repo", ".", "main"))


def test_quality_analyzer_passes_when_evidence_matches_acceptance():
    task = _task()
    result = AgentResult(
        task.task_id,
        "coder-1",
        TaskStatus.DONE,
        ResultOutput(
            summary="Implemented guarded truth validation",
            files_changed=["core/core/quality_analyzer.py"],
            commands_run=["pytest core/test/test_quality_analyzer.py -q"],
            test_results=[{"name": "truth validation", "status": "passed"}],
            diff="diff --git a/core/core/quality_analyzer.py b/core/core/quality_analyzer.py",
        ),
        0.92,
        [],
        [],
        provider="local",
        model_name="qwen2.5:32b-instruct-q4_k_m",
    )

    report = QualityAnalyzer().analyze(task, result)

    assert report.passed is True
    assert report.requires_review is False
    assert report.truth_basis
    assert "commands_run_present" in report.truth_basis
    assert "diff_present" in report.truth_basis


def test_quality_analyzer_requires_review_without_evidence():
    task = _task()
    result = AgentResult(
        task.task_id,
        "coder-1",
        TaskStatus.DONE,
        ResultOutput(summary="Looks done"),
        0.94,
        [],
        [],
        provider="local",
        model_name="qwen2.5:32b-instruct-q4_k_m",
    )

    report = QualityAnalyzer().analyze(task, result)

    assert report.passed is False
    assert report.requires_review is True
    assert "missing_verification_evidence" in report.issues
    assert "missing_diff_evidence" in report.issues
