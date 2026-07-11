from __future__ import annotations

from typing import Any

from .models import Complexity, ExecutionPlan, Priority, Task, TaskContext, TaskInput, TaskType

ANALYTICS_MULTI_AGENT_WAVE = "analytics-multi-agent-wave.v1"

_DOMAIN_KEYWORDS = (
    "analytics",
    "data analytics",
    "data platform",
    "analytics mart",
    "event model",
    "event sourcing",
    "feature store",
    "embedding store",
    "jsonb",
    "postgres",
    "routing metrics",
    "retrieval",
    "trained_memories",
    "memories",
    "ядро",
    "аналитик",
    "аналитика",
    "дата платформа",
    "витрин",
    "витрины",
    "событи",
)

_PARALLELIZATION_KEYWORDS = (
    "multi-agent",
    "multi agent",
    "parallel",
    "fanout",
    "split the code",
    "split code",
    "multiple agents",
    "between ai agents",
    "между ии агентами",
    "мультизадач",
    "параллел",
    "раздели задачу",
    "раздели код",
)

_TARGET_FILES = [
    "core/core/persistent_memory.py",
    "core/core/data_storage_analytics.py",
    "core/core/data_analytics_module.py",
    "core/core/hybrid_memory.py",
    "core/core/adaptive_routing_engine.py",
    "core/test/test_analytics_coding_orchestration.py",
]

_GLOBAL_CONSTRAINTS = [
    "Do not break the current memory contract for operational orchestration.",
    "Prefer additive schema and analytics changes over destructive refactors.",
    "Keep operational memory paths separate from analytics mart logic.",
    "Preserve deterministic fallback behavior when PostgreSQL analytics signals are incomplete.",
]


def _normalized_task_text(task: Task, advisory_context: dict[str, Any] | None = None) -> str:
    parts = [
        task.input.description or "",
        " ".join(task.input.files or []),
        " ".join(task.input.constraints or []),
        " ".join(task.input.acceptance_criteria or []),
        " ".join(str(v) for v in (task.routing_hints or {}).values()),
        " ".join(str(v) for v in (advisory_context or {}).values()),
    ]
    return " ".join(parts).lower()


def matches_analytics_multi_agent_request(
    task: Task, advisory_context: dict[str, Any] | None = None
) -> bool:
    if task.type not in {TaskType.PLAN, TaskType.CODE, TaskType.FIX, TaskType.REVIEW, TaskType.TEST}:
        return False
    if bool((task.routing_hints or {}).get("analytics_multi_agent")):
        return True
    text = _normalized_task_text(task, advisory_context=advisory_context)
    has_domain_signal = any(keyword in text for keyword in _DOMAIN_KEYWORDS)
    if not has_domain_signal:
        return False
    has_parallel_signal = any(keyword in text for keyword in _PARALLELIZATION_KEYWORDS)
    touches_target_files = any(path in text for path in _TARGET_FILES)
    return has_parallel_signal or touches_target_files


def _task(
    *,
    task_id: str,
    owner: str,
    task_type: TaskType,
    required_capability: str,
    description: str,
    files: list[str],
    constraints: list[str],
    acceptance_criteria: list[str],
    repo_path: str | None,
    branch: str | None,
    dependencies: list[str] | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        type=task_type,
        priority=Priority.HIGH,
        complexity=Complexity.MEDIUM,
        required_capability=required_capability,
        input=TaskInput(
            description=description,
            files=files,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
        ),
        context=TaskContext(project="core", repo_path=repo_path, branch=branch),
        dependencies=list(dependencies or []),
        routing_hints={
            "worker_role": owner,
            "parallel_group": "analytics_multi_agent_wave_1",
            "source": "analytics_coding_orchestration",
            "orchestration_wave": ANALYTICS_MULTI_AGENT_WAVE,
        },
    )


def build_analytics_multi_agent_execution_plan(task: Task) -> ExecutionPlan:
    lead_id = f"{task.task_id}-analytics-lead"
    events_id = f"{task.task_id}-events-schema"
    mart_id = f"{task.task_id}-analytics-mart"
    routing_id = f"{task.task_id}-routing-metrics"
    tests_id = f"{task.task_id}-analytics-tests"
    integrator_id = f"{task.task_id}-analytics-integrator"

    tasks = [
        _task(
            task_id=lead_id,
            owner="analytics_lead",
            task_type=TaskType.PLAN,
            required_capability="plan",
            description="Lead the CORE analytics implementation wave and freeze the integration contract for parallel workers.",
            files=_TARGET_FILES,
            constraints=_GLOBAL_CONSTRAINTS,
            acceptance_criteria=[
                "Parallel ownership boundaries are explicit.",
                "Integration checkpoints are defined.",
                "Cross-file contracts are frozen before parallel edits.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
        ),
        _task(
            task_id=events_id,
            owner="events_schema_owner",
            task_type=TaskType.CODE,
            required_capability="code_generation",
            description="Add additive analytics event and memory schema extensions for CORE persistence and retrieval paths.",
            files=["core/core/persistent_memory.py", "core/core/hybrid_memory.py"],
            constraints=_GLOBAL_CONSTRAINTS,
            acceptance_criteria=[
                "Schema additions are backward compatible.",
                "Operational memory writes still work for current callers.",
                "Analytics event fields are consumable downstream.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[lead_id],
        ),
        _task(
            task_id=mart_id,
            owner="analytics_mart_owner",
            task_type=TaskType.CODE,
            required_capability="code_generation",
            description="Build analytics-facing extraction or projection logic from CORE storage signals into stable marts and outcome-driven metrics.",
            files=["core/core/data_storage_analytics.py", "core/core/data_analytics_module.py"],
            constraints=_GLOBAL_CONSTRAINTS,
            acceptance_criteria=[
                "Readiness and freshness signals remain intact.",
                "Analytics projections expose stable outcome-driven metrics.",
                "Operational and analytics concerns stay separated.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[lead_id],
        ),
        _task(
            task_id=routing_id,
            owner="routing_metrics_owner",
            task_type=TaskType.CODE,
            required_capability="code_generation",
            description="Wire analytics metrics into routing and retrieval decisions without regressing cautious or degraded execution modes.",
            files=["core/core/data_analytics_module.py", "core/core/adaptive_routing_engine.py"],
            constraints=_GLOBAL_CONSTRAINTS,
            acceptance_criteria=[
                "Routing can consume outcome-driven analytics signals.",
                "Fallback routing behavior still works with partial analytics data.",
                "Retrieval gating remains explicit and auditable.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[lead_id],
        ),
        _task(
            task_id=tests_id,
            owner="analytics_test_owner",
            task_type=TaskType.TEST,
            required_capability="testing",
            description="Add targeted tests for the analytics multi-agent decomposition path and its worker dependencies.",
            files=[
                "core/test/test_analytics_coding_orchestration.py",
                "core/core/data_analytics_module.py",
                "core/core/data_storage_analytics.py",
            ],
            constraints=_GLOBAL_CONSTRAINTS,
            acceptance_criteria=[
                "The new orchestration path is covered by focused tests.",
                "Parallel dependencies are validated.",
                "Critical analytics regressions are covered.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[events_id, mart_id, routing_id],
        ),
        _task(
            task_id=integrator_id,
            owner="analytics_integrator",
            task_type=TaskType.REVIEW,
            required_capability="code_review",
            description="Integrate and review the parallel analytics wave output before final merge.",
            files=_TARGET_FILES,
            constraints=_GLOBAL_CONSTRAINTS,
            acceptance_criteria=[
                "Cross-track assumptions are resolved.",
                "Contract mismatches are identified before merge.",
                "The implementation is ready for coordinated rollout.",
            ],
            repo_path=task.context.repo_path,
            branch=task.context.branch,
            dependencies=[events_id, mart_id, routing_id, tests_id],
        ),
    ]
    return ExecutionPlan(
        root_task_id=lead_id,
        atomic_tasks=tasks,
        draft_layers=[
            {
                "name": "analytics_multi_agent_wave",
                "parallel": True,
                "objective": "Split CORE analytics and data-platform coding into parallel AI-agent tracks with explicit integration gates.",
            }
        ],
    )
