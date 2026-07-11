from core.core.analytics_matrix_orchestration import (
    ANALYTICS_MATRIX_MULTI_AGENT_WAVE,
    build_analytics_matrix_multi_agent_execution_plan,
    matches_analytics_matrix_multi_agent_request,
)
from core.core.models import Task, TaskContext, TaskInput, TaskType


def test_matches_analytics_matrix_multi_agent_request_for_parallel_request():
    task = Task(
        task_id="matrix-wave",
        type=TaskType.CODE,
        input=TaskInput(
            description="CORE: improve analytics matrix search and split the code between AI agents in parallel",
            files=["core/core/analytics_matrix_engine.py"],
        ),
        context=TaskContext(project="core", repo_path="."),
    )

    assert matches_analytics_matrix_multi_agent_request(task) is True


def test_build_analytics_matrix_multi_agent_execution_plan_encodes_parallel_lanes():
    task = Task(
        task_id="matrix-wave",
        type=TaskType.CODE,
        input=TaskInput(
            description="Split analytics matrix implementation between AI agents",
            files=["core/core/analytics_matrix_engine.py"],
        ),
        context=TaskContext(project="core", repo_path="."),
    )

    plan = build_analytics_matrix_multi_agent_execution_plan(task)
    tasks = {item.routing_hints.get("worker_role"): item for item in plan.atomic_tasks}

    assert plan.root_task_id == "matrix-wave-matrix-lead"
    assert tasks["matrix_engine_owner"].dependencies == ["matrix-wave-matrix-lead"]
    assert tasks["matrix_retrieval_owner"].dependencies == ["matrix-wave-matrix-lead"]
    assert tasks["matrix_generation_owner"].dependencies == ["matrix-wave-matrix-lead"]
    assert tasks["matrix_agent_owner"].dependencies == ["matrix-wave-matrix-lead"]
    assert tasks["matrix_test_owner"].dependencies == [
        "matrix-wave-matrix-core",
        "matrix-wave-matrix-retrieval",
        "matrix-wave-matrix-generator",
        "matrix-wave-matrix-agent",
    ]
    assert tasks["matrix_integrator"].routing_hints["orchestration_wave"] == ANALYTICS_MATRIX_MULTI_AGENT_WAVE
    assert plan.draft_layers[0]["parallel"] is True
