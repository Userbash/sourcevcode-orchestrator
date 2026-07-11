from core.core.analytics_coding_orchestration import (
    ANALYTICS_MULTI_AGENT_WAVE,
    build_analytics_multi_agent_execution_plan,
    matches_analytics_multi_agent_request,
)
from core.core.models import Task, TaskContext, TaskInput, TaskType


def test_matches_analytics_multi_agent_request_for_parallel_core_analytics_task():
    task = Task(
        task_id="analytics-wave",
        type=TaskType.CODE,
        input=TaskInput(
            description="CORE: implement data analytics mart and split the code between AI agents in parallel",
            files=["core/core/data_analytics_module.py"],
        ),
        context=TaskContext(project="core", repo_path="."),
    )

    assert matches_analytics_multi_agent_request(task) is True


def test_matches_analytics_multi_agent_request_rejects_unrelated_docs_task():
    task = Task(
        task_id="docs-task",
        type=TaskType.DOCS,
        input=TaskInput(
            description="Update README formatting",
            files=["README.md"],
        ),
        context=TaskContext(project="core", repo_path="."),
    )

    assert matches_analytics_multi_agent_request(task) is False


def test_analytics_multi_agent_execution_plan_encodes_parallel_lanes():
    task = Task(
        task_id="analytics-wave",
        type=TaskType.CODE,
        input=TaskInput(
            description="Split CORE analytics implementation between AI agents",
            files=["core/core/data_analytics_module.py"],
        ),
        context=TaskContext(project="core", repo_path="."),
    )

    plan = build_analytics_multi_agent_execution_plan(task)
    tasks = {item.routing_hints.get("worker_role"): item for item in plan.atomic_tasks}

    assert plan.root_task_id == "analytics-wave-analytics-lead"
    assert tasks["events_schema_owner"].dependencies == ["analytics-wave-analytics-lead"]
    assert tasks["analytics_mart_owner"].dependencies == ["analytics-wave-analytics-lead"]
    assert tasks["routing_metrics_owner"].dependencies == ["analytics-wave-analytics-lead"]
    assert tasks["analytics_test_owner"].dependencies == [
        "analytics-wave-events-schema",
        "analytics-wave-analytics-mart",
        "analytics-wave-routing-metrics",
    ]
    assert tasks["analytics_integrator"].dependencies == [
        "analytics-wave-events-schema",
        "analytics-wave-analytics-mart",
        "analytics-wave-routing-metrics",
        "analytics-wave-analytics-tests",
    ]
    assert plan.draft_layers[0]["parallel"] is True
    assert tasks["analytics_integrator"].routing_hints["orchestration_wave"] == ANALYTICS_MULTI_AGENT_WAVE
